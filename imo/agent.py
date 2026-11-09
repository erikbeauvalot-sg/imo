"""Moteur de l'agent pendant le call : enregistrement des messages et commandes « imv »."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from imo.analysis import TASKS, UsageError, analyze, contains_secret, format_transcript, local_hour
from imo.llm import LLM, LLMError
from imo.models import ChatMessage
from imo.sinks import Sink
from imo.storage import Store

log = logging.getLogger(__name__)

# Textes postés dans le chat, dans la langue des sous-titres de la réunion (« fr » ou « en »).
# Le rôle de l'agent, l'accueil et l'aide ne décrivent que ce qui est disponible : analyses si un
# LLM est configuré, recherche si un agent outillé l'est, voix sur Teams.

# Vocabulaire propre à chaque plateforme : (fr, en).
PLATFORMS = {
    "teams": {
        "label": ("réunion Teams", "Teams meeting"),
        "here": ("de ce chat", "of this chat"),
        "leave": ("je quitte la réunion", "I'm leaving the meeting"),
        "will_leave": ("je quitterai la réunion", "I will leave the meeting"),
        "names": ("celui affiché par Teams", "the one shown by Teams"),
        "blind": ("", ""),
    },
    "telegram": {
        "label": ("groupe Telegram", "Telegram group"),
        "here": ("de ce groupe", "of this group"),
        "leave": ("j'arrête de suivre ce groupe", "I'm no longer following this group"),
        "will_leave": ("j'arrêterai de suivre ce groupe", "I will stop following this group"),
        "names": ("celui affiché par Telegram, suivi de son identifiant (#…)",
                  "the one shown by Telegram, followed by its id (#…)"),
        "blind": (
            "Je ne vois que les commandes /… de ce groupe, pas la conversation : nommez-moi "
            "administrateur du groupe (ou désactivez mon « privacy mode » avec @BotFather, /setprivacy).",
            "I only see the /… commands of this group, not the conversation: make me an administrator "
            "of the group (or disable my privacy mode with @BotFather, /setprivacy).",
        ),
    },
}

# Aide : (condition, fr, en). Conditions : « llm » analyses disponibles, « no_llm » pas d'analyse,
# « research » agent de recherche configuré, « voice » voix transcrite possible (Teams).
_HELP_LINES = [
    (None, "Commandes IMV, à taper dans le chat :", "IMV commands, to type in the chat:"),
    (None, "imv démarrer [ID incident] : démarre le suivi de l'incident",
     "imv start [incident ID]: start following the incident"),
    ("llm", "imv résumé : point de situation", "imv summary: situation update"),
    ("llm", "imv catchup [HH:MM] : résumé court pour un nouvel arrivant, depuis une heure donnée",
     "imv catchup [HH:MM]: short summary for a newcomer, since a given time"),
    ("llm", "imv questions : questions à poser maintenant", "imv questions: questions to ask now"),
    ("llm", "imv pistes : hypothèses, vérifications et contournements",
     "imv leads: hypotheses, checks and workarounds"),
    ("llm", "imv timeline : chronologie des événements", "imv timeline: timeline of events"),
    ("llm", "imv actions : actions identifiées et owners", "imv actions: identified actions and owners"),
    ("llm", "imv com [direction|utilisateurs|tech] : brouillon de communication",
     "imv com [management|users|tech]: communication draft"),
    ("llm", "imv rapport : aperçu du résumé de fin", "imv report: preview of the final summary"),
    ("llm", "imv ? <question> : question libre sur l'incident", "imv ? <question>: free question about the incident"),
    ("research", "imv recherche <sujet> : confie une recherche web à {agent}, avec le chat en contexte",
     "imv research <topic>: hand a web search to {agent}, with the chat as context"),
    (None, "imv clore : clôture l'incident, publie le résumé de fin et arrête le suivi (responsable)",
     "imv close: close the incident, post the final summary and stop following (manager)"),
    (None, "imv statut : état de l'agent", "imv status: agent status"),
    (None, "imv pause / imv reprise : suspend / reprend l'enregistrement du chat",
     "imv pause / imv continue: stop / resume recording the chat"),
    (None, "imv autoriser <nom> | tous : autorise une personne, ou tout le monde, à utiliser IMV (responsable)",
     "imv allow <name> | all: allow a person, or everyone, to use IMV (manager)"),
    (None, "imv retirer <nom> | tous : retire une autorisation, ou réserve de nouveau IMV aux autorisés (responsable)",
     "imv revoke <name> | all: revoke an authorization, or restrict IMV to allowed people again (manager)"),
    (None, "imv autorisés : personnes autorisées", "imv allowed: allowed people"),
    (None, "imv aide : cette aide", "imv help: this help"),
    ("llm", "« imv » peut être remplacé par mon nom : « {name}, résumé », « {name}, quelles pistes ? »",
     "“imv” can be replaced by my name: “{name}, summary”, “{name}, what are the leads?”"),
    ("no_llm", "« imv » peut être remplacé par mon nom : « {name}, statut »",
     "“imv” can be replaced by my name: “{name}, status”"),
    ("voice", "À l'oral (si la voix est transcrite) : « {name}, fais un résumé », « {name}, quelles pistes ? »…",
     "Spoken (if the voice is transcribed): “{name}, give me a summary”, “{name}, what are the leads?”…"),
    ("llm", "Responsable : toute autre demande (« {name}, explique-moi… ») est une conversation libre avec moi, "
     "qui tient compte du contexte et de nos échanges précédents. Je réponds, je n'agis jamais à votre place.",
     "Manager: any other request (“{name}, explain…”) is a free conversation with me, which takes into "
     "account the context and our previous exchanges. I answer, I never act on your behalf."),
    ("no_llm", "Analyses (résumé, pistes, questions…) : non activées sur ce serveur.",
     "Analyses (summary, leads, questions…): not enabled on this server."),
]

# Intitulé des réponses selon le mode : 📋 analyse, 💬 conversation, 🔎 recherche (fr, en).
_ANALYSIS_LABELS = {
    "resume": ("Point de situation", "Situation update"),
    "catchup": ("Catch-up", "Catch-up"),
    "questions": ("Questions à poser", "Questions to ask"),
    "pistes": ("Pistes", "Leads"),
    "timeline": ("Chronologie", "Timeline"),
    "actions": ("Actions", "Actions"),
    "com": ("Brouillon de communication", "Communication draft"),
    "rapport": ("Aperçu du rapport", "Report preview"),
    "ask": ("Réponse", "Answer"),
}

# Formes canoniques (sans accents) des commandes de l'Étape 1.
_ALIASES = {
    "aide": "aide",
    "help": "aide",
    "demarrer": "demarrer",
    "start": "demarrer",
    "pause": "pause",
    "reprise": "reprise",
    "reprendre": "reprise",
    "statut": "statut",
    "status": "statut",
    "?": "ask",
    "autoriser": "autoriser",
    "allow": "autoriser",
    "retirer": "retirer",
    "revoke": "retirer",
    "autorises": "autorises",
    "acces": "autorises",
    "clore": "clore",
    "cloturer": "clore",
    "close": "clore",
    "recherche": "recherche",
    "research": "recherche",
    "hermes": "recherche",  # ancien nom de la commande, quand l'agent était forcément Hermes
    # Commandes en anglais (« resume » reste le résumé : « imv continue » pour reprendre)
    "summary": "resume",
    "leads": "pistes",
    "report": "rapport",
    "continue": "reprise",
    "unpause": "reprise",
    "allowed": "autorises",
}
# Commandes ouvertes à tous, et commandes réservées aux responsables.
_PUBLIC = {"aide"}
_MANAGER_ONLY = {"autoriser", "retirer", "clore", "clore_demande", "chat"}
_EVERYONE = {"tous", "tout le monde", "all", "everyone"}
# Commandes prévues plus tard : reconnues, mais pas encore disponibles.
_LATER: set[str] = set()
# Orthographe affichée des formes canoniques qui portent un accent.
_DISPLAY = {"demarrer": "démarrer", "resume": "résumé", "ask": "?", "chat": "?", "autorises": "autorisés"}
_DISPLAY_EN = {
    "aide": "help", "demarrer": "start", "resume": "summary", "pistes": "leads", "rapport": "report",
    "reprise": "continue", "statut": "status", "autoriser": "allow", "retirer": "revoke",
    "autorises": "allowed", "clore": "close", "ask": "?", "chat": "?", "recherche": "research",
}

_COMMAND = re.compile(r"^\s*imv\b[\s:,.!-]*(?P<word>\S*)\s*(?P<arg>.*)$", re.IGNORECASE | re.DOTALL)


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _name_key(name: str) -> str:
    """Comparaison des noms sans casse ni accents. Le suffixe « (Guest) » / « (Unverified) » est
    conservé : un invité peut choisir n'importe quel nom, ce suffixe le distingue de l'original."""
    return " ".join(_strip_accents(name).lower().split())


def _fingerprint(text: str) -> str:
    # Teams peut modifier espaces et retours à la ligne au rendu.
    return re.sub(r"\s+", "", text).lower()


@dataclass(frozen=True)
class Command:
    verb: str  # forme canonique sans accents (« demarrer »), ou le mot tel quel si inconnu
    word: str  # mot tapé, pour les réponses (« démarrer »)
    arg: str


def parse_command(text: str) -> Command | None:
    """« imv démarrer INC123 » → Command("demarrer", "démarrer", "INC123") ; None sinon."""
    m = _COMMAND.match(text)
    if m is None:
        return None
    word = m["word"]
    canonical = _strip_accents(word).lower()
    return Command(_ALIASES.get(canonical, canonical) or "aide", word or "aide", m["arg"].strip())


# Fin d'incident annoncée dans la conversation (chat ou oral, texte sans accents ni majuscules) :
# l'agent propose alors la clôture au responsable, sans jamais clore de lui-même.
_INCIDENT_OVER = re.compile(
    r"\b(incident|probleme|souci|panne|ticket)\s+(est\s+|semble\s+|bien\s+)*(clos|cloture|ferme|termine|fini|resolu|regle)\b"
    r"|\b(c'?est|tout est|tout c'?est)\s+(bien\s+)?(resolu|regle|rentre dans l'ordre)\b"
    r"|\b(service|site|appli|application)\s+(est\s+)?(retabli|revenu|de nouveau disponible)\b"
    r"|\bca (re)?(marche|fonctionne) (a nouveau|de nouveau)\b|\bca (re)?marche\b|\bca refonctionne\b"
    r"|\bon (peut|va) (clore|cloturer|fermer le pont|couper|raccrocher|terminer)\b"
    r"|\bfin de (l'?incident|la crise|la cellule)\b"
    r"|\b(issue|incident|problem)\s+(is\s+)?(resolved|fixed|closed|over)\b|\bwe can close\b"
)
# Négation dans la phrase (« pas encore résolu », « ça ne marche plus ») ; « plus » seul peut être
# positif (« ça remarche, plus d'erreurs »).
_NEGATION = re.compile(r"\b(pas|ne|not|never|jamais|aucunement)\b|\bn'|n't\b")
# Réponse d'un responsable à la proposition de clôture (même normalisation)
_YES = {"oui", "yes", "ok", "go", "vas-y", "vas y", "oui vas-y", "oui on clot", "oui clore", "on clot", "oui merci"}
CLOSE_PROPOSAL_DELAY = 600  # secondes : délai de réponse, et minimum entre deux propositions
CHAT_HISTORY = 6  # échanges de la conversation libre rappelés au LLM


def _plain(text: str) -> str:
    return " ".join(_strip_accents(text).lower().replace("’", "'").split())


# Commandes vocales : l'agent n'agit sur ce qui est dit que s'il est interpellé, par « IMV » ou par
# son nom (« Nova, un résumé »), éventuellement seul sur une ligne puis la demande dans la suivante.
# Les sous-titres transcrivent souvent « IMV » en « MV » ou « I M V ».
_WAKE_FILLERS = r"(?:(?:hey|hi|ok|okay|dis|eh|bon|alors|so|and|et)[\s,]+)?"
_IMV_SPOKEN = r"i[\s.]?m[\s.]?v|mv"
VOICE_FOLLOW_UP = 15  # secondes : demande attendue après un appel seul (« MV. »)

# Demande orale → commande (texte sans accents ni majuscules), par ordre de priorité.
# Pas d'autoriser / retirer à l'oral : les noms propres sont trop mal transcrits.
_VOICE_INTENTS = [(re.compile(p), verb) for p, verb in [
    (r"\b(close|clore|cloturer|cloture)\b", "clore"),
    (r"\b(pause|stop recording|arrete d'enregistrer)\b", "pause"),
    (r"\bcatch (me |us )?up\b|\bcatchup\b|\bmise a niveau\b|\brattrap", "catchup"),
    (r"\btimeline\b|\bchronolog", "timeline"),
    (r"\baction items?\b|\bactions\b|\bto ?do\b|\bqui fait quoi\b", "actions"),
    (r"\b(what|which) questions\b|\bquestions? (to ask|a poser)\b|\bquelles questions\b", "questions"),
    (r"\bhypothes|\bideas?\b|\bleads?\b|\bpistes?\b|\broot cause\b|\bsuggestions?\b", "pistes"),
    (r"\b(final )?report\b|\brapport\b", "rapport"),
    (r"\bcommunication\b|\bdraft\b|\bcom\b|\be-?mail\b", "com"),
    (r"\bsumm(ary|ari[sz]e)\b|\bsum up\b|\brecap\b|\bresume\b|\bpoint de situation\b|\bwhere are we\b"
     r"|\bou en est|\bfai[st] le point\b", "resume"),
    (r"\bhelp\b|\baide\b", "aide"),
]]
_VOICE_AUDIENCES = [
    (re.compile(r"\b(users?|utilisateurs?|clients?|customers?)\b"), "utilisateurs"),
    (re.compile(r"\b(tech|technical|technique|techniques)\b"), "tech"),
]


# Demandes écrites en langage naturel (« Oscar, quelles pistes ? ») : seules les demandes courtes
# sont prises pour une commande, les autres restent une conversation libre avec le responsable.
NATURAL_MAX_WORDS = 6
_NATURAL_VERBS = {"resume", "catchup", "questions", "pistes", "timeline", "actions", "rapport"}
_START_REQUEST = re.compile(r"^(?:demarre|demarrer|lance|start)\b")
_CLOSE_REQUEST = re.compile(r"^(?:clos|clore|cloture|cloturer|ferme|close)\b")
_INCIDENT_REF = re.compile(r"\b[A-Za-z]{2,}[-_]?\d+\b")


def parse_voice_request(text: str) -> Command | None:
    """« give me a summary please » → Command("resume", …) ; None si rien de reconnu."""
    plain = _plain(text)
    for pattern, verb in _VOICE_INTENTS:
        if pattern.search(plain):
            arg = ""
            if verb == "com":  # audience direction par défaut
                arg = next((a for p, a in _VOICE_AUDIENCES if p.search(plain)), "direction")
            return Command(verb, _DISPLAY.get(verb, verb), arg)
    return None


class IncidentAgent:
    """Traite le flux de messages d'une session : enregistrement, commandes, réponses."""

    def __init__(
        self,
        store: Store,
        session_id: int,
        display_name: str,
        send: Callable[[str], Awaitable[bool]],
        *,
        sink: Sink | None = None,
        llm: LLM | None = None,
        research: LLM | None = None,
        research_name: str = "Agent",
        context: str | None = None,
        managers: Iterable[str] = (),
        on_close: Callable[[], None] | None = None,
        language: str = "fr",
        platform: str = "teams",
        sees_all: Callable[[], bool] = lambda: True,
    ) -> None:
        self.store = store
        # Langue des messages postés et des analyses : celle des sous-titres de la réunion.
        self.language = language
        self.session_id = session_id
        self.display_name = display_name
        self.send = send
        self.sink = sink
        self.llm = llm
        # Agent outillé facultatif (Hermes ou autre) pour « imv recherche <sujet> », quel que soit le
        # LLM des analyses ; `research_name` : son nom dans les messages.
        self.research = research
        self.research_name = research_name
        # Plateforme de la conversation (« teams », « telegram ») : vocabulaire des messages.
        self.platform = platform if platform in PLATFORMS else "teams"
        # False si l'agent ne voit que les commandes (bot Telegram en « privacy mode »).
        self.sees_all = sees_all
        # Contexte de l'entreprise (fichier --context), transmis au LLM avec chaque analyse.
        self.context = context
        self.paused = False
        # Analyses en cours : exécutées en tâche de fond pour ne pas bloquer la lecture du chat.
        self._analyses: set[asyncio.Task] = set()
        # Empreintes des messages envoyés, pour ne pas les relire comme des messages humains.
        self._own: deque[str] = deque(maxlen=50)
        # Autorisations : les responsables (--im, sinon le premier à taper une commande) gèrent
        # la liste des personnes autorisées ; clé normalisée → nom tel qu'affiché.
        self.managers: dict[str, str] = {_name_key(n): n for n in managers if n.strip()}
        self.allowed: dict[str, str] = {}
        self.open_to_all = False
        self._seen: dict[str, str] = {}  # auteurs vus dans le chat, pour compléter « imv autoriser »
        self._refused: set[str] = set()  # refus déjà expliqués : une seule réponse par personne
        # Voix transcrite (sous-titres Teams) : annoncée dans l'accueil.
        self.transcribing = False
        # Clôture : on_close fait quitter la réunion (runner) une fois le résumé de fin publié.
        self.on_close = on_close
        self.closing = False
        self._close_proposed_at: float | None = None
        # Nom qui interpelle l'agent (« IMV - Oscar » → « Oscar »), à l'oral comme dans le chat.
        self.name = display_name.split(" - ")[-1].strip() or "IMV"
        own = _plain(self.name)
        names = _IMV_SPOKEN + (f"|{re.escape(own)}" if own != "imv" else "")
        self._wake = re.compile(rf"^{_WAKE_FILLERS}(?:{names})\b[\s,.:;!?-]*(?P<rest>.*)$")
        # Dans le chat : « Oscar, … », « Oscar : … » ou « @Oscar … » (mention Teams, éventuellement
        # avec le nom complet « IMV - Oscar ») ; le nom seul suivi d'un espace peut désigner une
        # personne (« Oscar a relancé le serveur »).
        self._chat_wake = re.compile(
            rf"^\s*(?P<at>@)?\s*(?:imv\s*-\s*)?{re.escape(self.name)}\b\s*(?P<sep>[,:!?]?)\s*(?P<rest>.*)$",
            re.IGNORECASE | re.DOTALL,
        )
        self._called_at: dict[str, float] = {}  # orateur → heure d'un appel seul, en attente de la demande
        # Conversation libre des responsables : (auteur, demande, réponse), pour les questions de suivi.
        self._chat_history: deque[tuple[str, str, str]] = deque(maxlen=CHAT_HISTORY)

    def _t(self, fr: str, en: str) -> str:
        """Texte dans la langue de la réunion."""
        return en if self.language == "en" else fr

    def _cmd_name(self, verb: str) -> str:
        """Commande telle qu'affichée dans la langue de la réunion (« résumé » / « summary »)."""
        return (_DISPLAY_EN if self.language == "en" else _DISPLAY).get(verb, verb)

    def _p(self, key: str) -> str:
        """Vocabulaire de la plateforme, dans la langue de la réunion."""
        return self._t(*PLATFORMS[self.platform][key])

    def help_text(self) -> str:
        """Aide limitée à ce qui est disponible (analyses, recherche, voix)."""
        conditions = {
            None: True,
            "llm": self.llm is not None,
            "no_llm": self.llm is None,
            "research": self.research is not None,
            "voice": self.platform == "teams",
        }
        return "\n".join(
            self._t(fr, en).format(name=self.name, agent=self.research_name)
            for condition, fr, en in _HELP_LINES
            if conditions[condition]
        )

    def welcome_text(self) -> str:
        """Accueil : qui est l'agent, ce qu'il enregistre, comment lui parler."""
        lines = [self._t(
            f"Bonjour, je suis {self.name}, l'assistant de l'Incident Manager (IMV). J'enregistre les messages "
            f"{self._p('here')} sur un serveur interne pour aider au suivi de l'incident. Je ne décide rien et "
            "n'agis sur aucun système : je propose, l'équipe décide.",
            f"Hello, I'm {self.name}, the Incident Manager's assistant (IMV). I record the messages "
            f"{self._p('here')} on an internal server to help follow up on the incident. I don't decide "
            "anything and don't act on any system: I suggest, the team decides.",
        )]
        if self.llm is not None:
            lines.append(self._t(
                f"Pour me parler : une commande (« imv résumé », « {self.name}, un résumé », « {self.name}, "
                f"quelles pistes ? ») ou, pour le responsable, une question libre (« {self.name}, explique-moi… »).",
                f"To talk to me: a command (“imv summary”, “{self.name}, a summary”, “{self.name}, what are "
                f"the leads?”) or, for the manager, a free question (“{self.name}, explain…”).",
            ))
        else:
            lines.append(self._t(
                f"Pour me parler : « imv démarrer <ID> », « imv statut » ou « {self.name}, statut ». "
                "Les analyses (résumés, pistes) ne sont pas activées sur ce serveur.",
                f"To talk to me: “imv start <ID>”, “imv status” or “{self.name}, status”. "
                "Analyses (summaries, leads) are not enabled on this server.",
            ))
        if self.research is not None:
            lines.append(self._t(
                f"« imv recherche <sujet> » confie une recherche web à {self.research_name}.",
                f"“imv research <topic>” hands a web search to {self.research_name}.",
            ))
        lines.append(self._t(
            "« imv pause » suspend l'enregistrement, « imv aide » liste les commandes.",
            "“imv pause” stops recording, “imv help” lists the commands.",
        ))
        return "\n".join(lines)

    def situation(self, verb: str = "") -> str:
        """État réel de la session et capacités disponibles, transmis au LLM avec chaque demande :
        il ne doit ni inventer un état (« suivi déjà démarré ») ni proposer ce qui n'existe pas."""
        incident = self.store.session_incident(self.session_id)
        if incident is None:
            follow_up = "suivi non démarré (commande « imv démarrer <ID> »)"
        else:
            follow_up = f"{incident['ref'] or 'sans référence'}, suivi démarré"
        access = "ouvertes à tous" if self.open_to_all else "réservées aux responsables et aux personnes autorisées"
        managers = self._managers_label() or "aucun pour l'instant"
        lines = [
            f"- Conversation suivie : {PLATFORMS[self.platform]['label'][0]}",
            f"- Incident : {follow_up}",
            f"- Responsable(s) d'IMV : {managers} ; commandes {access}",
            f"- Enregistrement : {'en pause' if self.paused else 'actif'}",
        ]
        if not self.sees_all():
            lines.append(
                "- Tu ne vois que les commandes tapées dans la conversation, pas les autres messages : si on te "
                "demande une analyse, explique qu'il faut te nommer administrateur du groupe."
            )
        if verb == "recherche":
            lines.append("- Cette demande est une recherche : tu peux te servir de tes outils.")
        elif self.research is not None:
            lines.append(
                f"- Recherche web : tu n'y as pas accès toi-même ; les participants peuvent la confier à "
                f"{self.research_name} avec « imv recherche <sujet> »."
            )
        else:
            lines.append(
                "- Recherche web : aucune sur ce serveur. Ne propose ni recherche web ni agent externe ; "
                "appuie-toi sur le chat, le contexte et ton expertise."
            )
        commands = [f"imv {self._cmd_name('demarrer')} <ID>"]
        if self.llm is not None:
            commands += [f"imv {self._cmd_name(v)}" for v in ("resume", "catchup", "questions", "pistes", "timeline", "actions")]
            commands += ["imv com <audience>", f"imv {self._cmd_name('rapport')}", "imv ? <question>"]
        if self.research is not None:
            commands.append(f"imv {self._cmd_name('recherche')} <sujet>")
        commands += [f"imv {self._cmd_name(v)}" for v in ("statut", "pause", "reprise", "autorises", "aide")]
        commands += [
            f"imv {self._cmd_name('autoriser')} <nom> (responsable)",
            f"imv {self._cmd_name('clore')} (responsable)",
        ]
        lines.append("- Commandes que les participants peuvent taper : " + ", ".join(commands))
        return "\n".join(lines)

    async def welcome(self) -> bool:
        text = self.welcome_text()
        if self.transcribing:
            text += self._t(
                "\nJe transcris aussi ce qui est dit à l'oral, à partir des sous-titres Teams ; "
                "« imv pause » suspend aussi cette transcription. Pour me parler à l'oral, "
                f"interpellez-moi : « {self.name}, un résumé s'il te plaît ».",
                "\nI also transcribe what is said aloud, from the Teams captions; "
                "“imv pause” also stops this transcription. To talk to me, "
                f"call me: “{self.name}, a summary please”.",
            )
        if self.managers:
            text += self._t(
                f"\nCommandes réservées à {self._managers_label()}, qui peut autoriser d'autres participants "
                "(« imv autoriser <nom> » ou « imv autoriser tous »).",
                f"\nCommands reserved to {self._managers_label()}, who can allow other participants "
                "(“imv allow <name>” or “imv allow all”).",
            )
        if not self.sees_all():
            text += f"\n{self._p('blind')}"
        return await self.reply(text, kind="welcome")

    async def farewell(self) -> bool:
        """Au revoir posté avant de quitter la réunion (bouton « Quitter la réunion » de l'interface)."""
        leave = self._p("leave")
        return await self.reply(
            self._t(
                f"{leave[0].upper()}{leave[1:]} : le suivi par IMV s'arrête ici. Merci à tous.",
                f"{leave[0].upper()}{leave[1:]}: IMV follow-up stops here. Thank you all.",
            ),
            kind="farewell",
        )

    def start_incident(self, ref: str | None, im_name: str | None = None) -> None:
        self.store.start_incident(self.session_id, ref, im_name)
        log.info("Suivi de l'incident %s démarré", ref or "(sans référence)")

    async def consume(self, queue: asyncio.Queue[ChatMessage]) -> None:
        while True:
            msg = await queue.get()
            try:
                await self.handle(msg)
            except Exception:  # noqa: BLE001 — un message en erreur ne doit pas arrêter l'agent
                log.exception("Erreur de traitement du message %r", msg.text)

    async def handle(self, msg: ChatMessage) -> None:
        if self._is_own(msg):
            return
        if not self.paused and self.store.add_message(self.session_id, msg) and self.sink:
            self.sink.write(msg)
        if msg.backlog or msg.source not in ("chat", "transcript"):
            return
        if msg.source == "transcript":
            if not self.paused and not await self._voice_command(msg):
                await self._propose_close_if_over(msg)
            return
        self._seen.setdefault(_name_key(msg.sender), msg.sender)
        command = parse_command(msg.text) or self._named_command(msg.text)
        if command is not None:
            await self.execute(self._as_chat(self._route(command), msg.sender), msg.sender)
        elif self._accepts_close(msg):
            await self.execute(Command("clore", "clore", ""), msg.sender)
        elif await self._propose_close_if_over(msg):
            pass
        elif not self.paused and contains_secret(msg.text):
            await self.reply(
                self._t(
                    f"Attention : le message de {msg.sender} semble contenir un mot de passe ou un jeton. "
                    "Supprimez-le du chat et faites changer ce secret ; partagez-le plutôt par un canal sécurisé. "
                    "(Il n'est pas transmis à l'analyse.)",
                    f"Warning: {msg.sender}'s message seems to contain a password or a token. "
                    "Delete it from the chat and have this secret changed; share it through a secure channel instead. "
                    "(It is not sent for analysis.)",
                ),
                kind="proactive",
                rule_id="secret_posted",
            )

    async def execute(self, command: Command, author: str, heard: str | None = None) -> None:
        """`heard` : phrase dite à l'oral, rappelée dans le chat pour montrer ce qui a été compris."""
        if not await self._authorize(command, author):
            return
        if heard is not None:
            quote = heard if len(heard) <= 80 else heard[:77] + "…"
            arg = f" {command.arg}" if command.arg and command.verb not in ("ask", "chat") else ""
            await self.reply(self._t(
                f"{author} (à l'oral) : « {quote} » → imv {self._cmd_name(command.verb)}{arg}",
                f"{author} (spoken): “{quote}” → imv {self._cmd_name(command.verb)}{arg}",
            ))
        if command.verb == "clore":
            self._background(self._close(author))
            return
        if command.verb == "clore_demande":  # « Oscar, clos l'incident » : confirmation d'abord
            self._close_proposed_at = asyncio.get_running_loop().time()
            await self.reply(self._t(
                f"{author}, je clos l'incident ? Répondez « oui » ou tapez « imv clore » : je publierai le "
                f"résumé de fin, puis {self._p('will_leave')}.",
                f"{author}, shall I close the incident? Answer “yes” or type “imv close”: I will post the "
                f"final summary, then {self._p('will_leave')}.",
            ))
            return
        if command.verb in TASKS:
            if self._llm_for(command.verb) is None:
                await self.reply(self._unavailable(command.verb))
                return
            if command.verb == "recherche" and command.arg.strip():
                await self.reply(self._t(
                    f"Recherche confiée à {self.research_name} : réponse dans quelques instants.",
                    f"Search handed to {self.research_name}: answer in a few moments.",
                ))
            self._background(self._analyze(command, author))
            return
        handler = getattr(self, f"_cmd_{command.verb}", None)
        if handler is not None:
            text = handler(command.arg, author)
        elif command.verb in _LATER:
            text = self._t(f"« imv {command.word} » n'est pas encore disponible.", f"“imv {command.word}” is not available yet.")
        else:
            text = self._t(f"Commande inconnue : « {command.word} ».", f"Unknown command: “{command.word}”.")
            close = difflib.get_close_matches(command.verb, [*_ALIASES, *TASKS, *_LATER], n=1, cutoff=0.6)
            if close:
                verb = self._cmd_name(_ALIASES.get(close[0], close[0]))
                text += self._t(f" Vouliez-vous dire « imv {verb} » ?", f" Did you mean “imv {verb}”?")
            text += self._t(" Tapez « imv aide » pour la liste.", " Type “imv help” for the list.")
        await self.reply(text)

    def _named_command(self, text: str) -> Command | None:
        """« Oscar, résumé » → comme « imv résumé » ; None si le message ne s'adresse pas à l'agent."""
        if _plain(self.name) == "imv":
            return None  # déjà couvert par parse_command
        m = self._chat_wake.match(text)
        if m is None or not (m["at"] or m["sep"] or not m["rest"].strip()):
            return None
        return parse_command(f"imv {m['rest']}")

    def _route(self, command: Command) -> Command:
        """Demande écrite en langage naturel (« imv quelles pistes ? », « Oscar, démarre INC1 ») :
        la commande correspondante si la demande est courte et sans ambiguïté, sinon telle quelle
        (conversation libre ou commande inconnue)."""
        if command.verb == "ask" or self._known(command.verb):
            return command
        text = f"{command.word} {command.arg}".strip()
        plain = _plain(text).strip(" .!?")
        if not plain or len(plain.split()) > NATURAL_MAX_WORDS:
            return command
        if _START_REQUEST.match(plain):
            # « démarre INC1 », « démarre le suivi » ; pas « lance une recherche »
            ref = _INCIDENT_REF.search(text)
            if ref or "suivi" in plain.split():
                return Command("demarrer", self._cmd_name("demarrer"), ref.group(0) if ref else "")
            return command
        if _CLOSE_REQUEST.match(plain):
            return Command("clore_demande", self._cmd_name("clore"), "")
        natural = parse_voice_request(plain)
        if natural is not None and natural.verb in _NATURAL_VERBS:
            return natural
        return command

    def _known(self, verb: str) -> bool:
        return verb in TASKS or verb in _LATER or verb in ("clore", "clore_demande") or hasattr(self, f"_cmd_{verb}")

    def _manager_or_first(self, author: str) -> bool:
        """Responsable, ou première personne à s'adresser à l'agent sans responsable désigné :
        _authorize la désigne alors responsable, sa demande doit déjà être traitée comme telle."""
        return not self.managers or _name_key(author) in self.managers

    def _as_chat(self, command: Command, author: str) -> Command:
        """« imv <demande libre> » ou « imv ? … » d'un responsable → conversation libre. Une faute de
        frappe sur une commande (« imv resmué », « imv demarer INC1 ») garde sa suggestion."""
        if self.llm is None or not self._manager_or_first(author):
            return command
        if command.verb == "ask":
            return Command("chat", "?", command.arg) if command.arg.strip() else command
        if self._known(command.verb):
            return command
        # Seuil plus strict que la suggestion : « salut » ne doit pas passer pour « statut ».
        typo = difflib.get_close_matches(command.verb, [*_ALIASES, *TASKS, *_LATER], n=1, cutoff=0.8)
        if typo and len(command.arg.split()) <= 1:
            return command
        return Command("chat", "?", f"{command.word} {command.arg}".strip())

    async def _authorize(self, command: Command, author: str) -> bool:
        if command.verb in _PUBLIC:
            return True
        key = _name_key(author)
        if not self.managers:
            self.managers[key] = author
            log.info("Responsable IMV : %s (première commande)", author)
            await self.reply(self._t(
                f"{author} est responsable d'IMV (première personne à l'utiliser) : les commandes lui sont "
                "réservées. « imv autoriser <nom> » ou « imv autoriser tous » pour en ouvrir l'accès.",
                f"{author} is the IMV manager (first person to use it): commands are reserved to them. "
                "“imv allow <name>” or “imv allow all” to open access.",
            ))
            return True
        if key in self.managers:
            return True
        if command.verb not in _MANAGER_ONLY and (self.open_to_all or key in self.allowed):
            return True
        log.info("Commande « imv %s » refusée à %s", command.word, author)
        if key not in self._refused:
            self._refused.add(key)
            manager_only = command.verb in _MANAGER_ONLY
            word = self._cmd_name(command.verb) if command.verb in ("ask", "chat") else command.word
            await self.reply(self._t(
                f"{author}, « imv {word} » est réservé "
                f"{'aux responsables' if manager_only else 'aux personnes autorisées'} d'IMV. "
                f"{self._managers_label()} peut vous ajouter : imv autoriser {author}",
                f"{author}, “imv {word}” is reserved to {'IMV managers' if manager_only else 'allowed people'}. "
                f"{self._managers_label()} can add you: imv allow {author}",
            ))
        return False

    def _managers_label(self) -> str:
        return self._t(" ou ", " or ").join(self.managers.values())

    def _resolve_name(self, arg: str) -> tuple[str | None, str]:
        """Nom à autoriser : complété d'après les auteurs vus (« Sophie » → « Sophie Lambert »).
        Renvoie (nom, remarque) ; nom à None si plusieurs participants correspondent."""
        name = arg.strip().lstrip("@").strip()
        key = _name_key(name)
        if key in self._seen:
            return self._seen[key], ""
        words = key.split()
        matches = [n for k, n in self._seen.items() if all(w in k.split() for w in words)]
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            return None, self._t(
                f"Plusieurs participants correspondent : {', '.join(matches)}. Précisez le nom.",
                f"Several participants match: {', '.join(matches)}. Please specify the name.",
            )
        return name, self._t(
            f" Cette personne n'a pas encore écrit dans le chat : son nom doit être exactement {self._p('names')}.",
            f" This person hasn't written in the chat yet: their name must be exactly {self._p('names')}.",
        )

    async def api_command(self, text: str, requester: str) -> tuple[Command, str, bool]:
        """Commande envoyée par l'API du serveur web (« résumé », « com direction », « clore »…),
        sans contrôle des responsables de la réunion : les droits sont ceux de l'utilisateur du
        serveur. La réponse est publiée dans le chat ; renvoie (commande, réponse, publiée).
        ValueError si la commande est inconnue ou impossible."""
        command = parse_command(f"imv {text}")
        if command is None or not text.strip() or not self._known(command.verb):
            raise ValueError(f"commande inconnue : {text.strip()!r} (voir « aide »)")
        log.info("Commande « imv %s » reçue par l'API (%s)", command.word, requester)
        if command.verb == "clore":
            if self.closing:
                raise ValueError("clôture déjà en cours")
            self._background(self._close(requester))
            return command, self._t("Clôture en cours.", "Closing in progress."), True
        if command.verb in TASKS:
            if self._llm_for(command.verb) is None:
                raise ValueError(
                    "aucun agent de recherche n'est configuré sur ce serveur (IMV_AGENT_URL et IMV_AGENT_KEY, "
                    "ou HERMES_API_KEY)" if command.verb == "recherche"
                    else "l'analyse n'est pas activée pour cette session (pas de LLM)"
                )
            text = await self._analysis_text(command, requester)
        else:
            text = getattr(self, f"_cmd_{command.verb}")(command.arg, requester)
        return command, text, await self.reply(text, kind="api")

    async def _analyze(self, command: Command, author: str) -> None:
        await self.reply(await self._analysis_text(command, author))

    def _llm_for(self, verb: str) -> LLM | None:
        return self.research if verb == "recherche" else self.llm

    def _unavailable(self, verb: str) -> str:
        if verb == "recherche":
            if self.llm is None:
                return self._t(
                    "Aucune recherche web n'est disponible sur ce serveur.",
                    "No web search is available on this server.",
                )
            return self._t(
                "Aucune recherche web n'est disponible sur ce serveur : « imv pistes » ou « imv ? <question> » "
                "pour une analyse à partir du chat.",
                "No web search is available on this server: “imv leads” or “imv ? <question>” for an "
                "analysis based on the chat.",
            )
        return self._t(
            "L'analyse n'est pas activée pour cette session (option --llm).",
            "Analysis is not enabled for this session (--llm option).",
        )

    def _labelled(self, verb: str, text: str) -> str:
        """Réponse marquée selon le mode : 📋 analyse, 💬 conversation, 🔎 recherche."""
        if verb == "chat":
            return f"💬 {text}"
        if verb == "recherche":
            return f"🔎 {self._t('Recherche', 'Search')} ({self.research_name})\n{text}"
        fr, en = _ANALYSIS_LABELS.get(verb, (verb, verb))
        return f"📋 {self._t(fr, en)}\n{text}"

    async def _analysis_text(self, command: Command, author: str) -> str:
        llm = self._llm_for(command.verb)
        assert llm is not None
        incident = self.store.session_incident(self.session_id)
        messages = self.store.messages(self.session_id)
        if command.verb not in ("chat", "recherche") and not format_transcript(messages):
            if not self.sees_all():  # rien à analyser parce que l'agent ne voit pas la conversation
                return self._p("blind")
            return self._t("Aucun message à analyser pour l'instant.", "No messages to analyze yet.")
        try:
            text = await analyze(
                llm,
                command.verb,
                messages,
                incident["ref"] if incident else None,
                command.arg,
                self.context,
                requester=author,
                agent_name=self.display_name,
                history=self._chat_history if command.verb == "chat" else (),
                language=self.language,
                name=self.name,
                situation=self.situation(command.verb),
            )
            if command.verb == "chat" and text:
                self._chat_history.append((author, command.arg, text))
        except UsageError as exc:
            return str(exc)
        except LLMError as exc:
            log.error("Analyse « %s » impossible : %s", command.word, exc)
            return self._t(
                f"Analyse « {command.word} » impossible pour le moment ({exc}).",
                f"“{command.word}” analysis unavailable for now ({exc}).",
            )
        if not text:
            return self._t(f"Analyse « {command.word} » : réponse vide.", f"“{command.word}” analysis: empty answer.")
        return self._labelled(command.verb, text)

    def _background(self, coro) -> None:
        """Analyse LLM en tâche de fond : la lecture du chat continue pendant ce temps."""
        task = asyncio.create_task(coro)
        self._analyses.add(task)
        task.add_done_callback(self._analyses.discard)

    # -- commandes vocales ------------------------------------------------

    async def _voice_command(self, msg: ChatMessage) -> bool:
        """Exécute une demande orale adressée à l'agent. True si la phrase lui était adressée."""
        key, now = _name_key(msg.sender), asyncio.get_running_loop().time()
        heard = msg.text.strip()
        m = self._wake.match(_plain(heard))
        if m is None:
            called = self._called_at.pop(key, None)
            if called is None or now - called > VOICE_FOLLOW_UP:
                return False
            request = heard  # demande dans la ligne qui suit l'appel
        else:
            self._called_at.pop(key, None)
            request = m["rest"]
            if not request.strip(" .,!?"):
                self._called_at[key] = now
                log.info("Appel vocal de %s : demande attendue", msg.sender)
                return True
        command = parse_voice_request(request)
        if command is None:
            if _INCIDENT_OVER.search(_plain(request)):
                return False  # « IMV, l'incident est résolu » : proposition de clôture
            if self.llm is None:
                await self.reply(self._t(
                    f"{msg.sender}, je n'ai pas compris « {heard} ». "
                    f"Dites par exemple « {self.name}, un résumé » ou tapez « imv aide ».",
                    f"{msg.sender}, I didn't understand “{heard}”. "
                    f"Say for example “{self.name}, a summary” or type “imv help”.",
                ))
                return True
            if self._manager_or_first(msg.sender):
                command = Command("chat", "?", heard)  # conversation libre du responsable
            else:
                command = Command("ask", "?", heard)  # question libre
        await self.execute(command, msg.sender, heard=heard)
        return True

    # -- clôture ----------------------------------------------------------

    async def _propose_close_if_over(self, msg: ChatMessage) -> bool:
        """Fin d'incident évoquée : propose la clôture au responsable, au plus une fois par
        CLOSE_PROPOSAL_DELAY. True si la proposition a été faite."""
        text = _plain(msg.text)
        if self.paused or self.closing or not _INCIDENT_OVER.search(text) or _NEGATION.search(text):
            return False
        now = asyncio.get_running_loop().time()
        if self._close_proposed_at is not None and now - self._close_proposed_at < CLOSE_PROPOSAL_DELAY:
            return False
        self._close_proposed_at = now
        quote = msg.text if len(msg.text) <= 80 else msg.text[:77] + "…"
        oral = self._t(" (à l'oral)", " (spoken)") if msg.source == "transcript" else ""
        if self.managers:
            ask = self._t(
                f"{self._managers_label()}, voulez-vous clore l'incident ? Répondez « oui » ou tapez « imv clore »",
                f"{self._managers_label()}, do you want to close the incident? Answer “yes” or type “imv close”",
            )
        else:
            ask = self._t(
                "Le responsable de l'incident peut le clore en tapant « imv clore »",
                "The incident manager can close it by typing “imv close”",
            )
        log.info("Fin d'incident évoquée par %s : proposition de clôture", msg.sender)
        await self.reply(
            self._t(
                f"{msg.sender}{oral} : « {quote} ». L'incident semble terminé. {ask} : je publierai le "
                f"résumé de fin, puis {self._p('will_leave')}.",
                f"{msg.sender}{oral}: “{quote}”. The incident seems over. {ask}: I will post the "
                f"final summary, then {self._p('will_leave')}.",
            ),
            kind="proactive",
            rule_id="incident_over",
        )
        return True

    def _accepts_close(self, msg: ChatMessage) -> bool:
        """« oui » d'un responsable peu après la proposition de clôture."""
        if self._close_proposed_at is None or self.closing or _name_key(msg.sender) not in self.managers:
            return False
        if asyncio.get_running_loop().time() - self._close_proposed_at > CLOSE_PROPOSAL_DELAY:
            return False
        return _plain(msg.text).strip(" .!") in _YES

    async def _close(self, author: str) -> None:
        if self.closing:
            await self.reply(self._t("Clôture déjà en cours.", "Closing already in progress."))
            return
        self.closing = True
        log.info("Clôture de l'incident demandée par %s", author)
        if self.llm is not None:
            await self.reply(self._t(
                f"Clôture demandée par {author} : je rédige le résumé de fin…",
                f"Closing requested by {author}: writing the final summary…",
            ))
        await self.reply(await self._final_summary(), kind="report")
        self.store.close_incident(self.session_id)
        await self.reply(self._t(
            f"Fin du suivi de l'incident par IMV. {self._p('leave').capitalize()}, merci à tous.",
            f"End of incident follow-up by IMV. {self._p('leave')}, thank you all.",
        ))
        if self.on_close is not None:
            self.on_close()

    async def _final_summary(self) -> str:
        incident = self.store.session_incident(self.session_id)
        ref = incident["ref"] if incident else None
        messages = self.store.messages(self.session_id)
        if self.llm is not None:
            try:
                text = await analyze(
                    self.llm, "rapport", messages, ref, context=self.context, agent_name=self.display_name,
                    language=self.language, name=self.name, situation=self.situation("rapport"),
                )
                if text.strip():
                    return text
            except LLMError as exc:
                log.error("Résumé de fin impossible : %s", exc)
        # Sans LLM (ou en erreur) : faits enregistrés, sans interprétation.
        human = [m for m in messages if m["source"] != "system" and parse_command(m["text"]) is None]
        people = sorted({m["author_name"] for m in human})
        lines = [self._t("Résumé de fin", "Final summary") + (f" — incident {ref}" if ref else "")]
        if human:
            start, end = (human[0]["sent_at"] or human[0]["captured_at"]), (human[-1]["sent_at"] or human[-1]["captured_at"])
            s = "s" if len(human) > 1 else ""
            lines.append(self._t(
                f"• Échanges enregistrés : {len(human)} message{s}, de {local_hour(start)} à {local_hour(end)}",
                f"• Recorded exchanges: {len(human)} message{s}, from {local_hour(start)} to {local_hour(end)}",
            ))
            lines.append(self._t("• Participants : ", "• Participants: ") + ", ".join(people))
        else:
            lines.append(self._t("• Aucun message enregistré.", "• No recorded messages."))
        lines.append(self._t(
            "Analyse non disponible (pas de LLM) : relisez l'historique avec « imo_agent.py history ».",
            "Analysis unavailable (no LLM): review the history with “imo_agent.py history”.",
        ))
        return "\n".join(lines)

    async def wait_analyses(self) -> None:
        """Attend la fin des analyses en cours (fin de session, tests)."""
        if self._analyses:
            await asyncio.gather(*self._analyses, return_exceptions=True)

    async def reply(self, text: str, *, kind: str = "command", rule_id: str | None = None) -> bool:
        fingerprint = _fingerprint(text)
        self._own.append(fingerprint)
        delivered = await self.send(text)
        self.store.add_intervention(self.session_id, kind, text, delivered, rule_id)
        if delivered:
            log.info("%s → chat : %s", self.name, text.splitlines()[0])
        else:
            self._own.remove(fingerprint)
            log.warning("Réponse non publiée (chat en lecture seule ?) :\n%s", text)
        return delivered

    def _is_own(self, msg: ChatMessage) -> bool:
        # Teams peut suffixer le nom d'un invité (« (Guest) », « (Unverified) »)
        # ou afficher ses propres messages sans auteur.
        if msg.sender.startswith(self.display_name):
            return True
        fingerprint = _fingerprint(msg.text)
        if fingerprint in self._own:
            self._own.remove(fingerprint)
            return True
        return False

    # -- commandes --------------------------------------------------------

    def _cmd_aide(self, arg: str, author: str) -> str:
        return self.help_text()

    def _cmd_demarrer(self, arg: str, author: str) -> str:
        incident = self.store.session_incident(self.session_id)
        if incident is not None:
            return self._t(
                f"Suivi déjà démarré : {incident['ref'] or 'incident sans référence'}.",
                f"Follow-up already started: {incident['ref'] or 'incident without reference'}.",
            )
        ref = arg.split()[0] if arg else None
        self.start_incident(ref, author)
        return self._t(
            f"Suivi de l'incident {ref or '(sans référence)'} démarré, IM : {author}. "
            "Les messages déjà captés y sont rattachés.",
            f"Follow-up of incident {ref or '(no reference)'} started, IM: {author}. "
            "Messages already captured are attached to it.",
        )

    def _cmd_pause(self, arg: str, author: str) -> str:
        if self.paused:
            return self._t(
                "L'enregistrement est déjà en pause. « imv reprise » pour reprendre.",
                "Recording is already paused. “imv continue” to resume.",
            )
        self.paused = True
        return self._t(
            "Enregistrement du chat en pause. « imv reprise » pour reprendre.",
            "Chat recording paused. “imv continue” to resume.",
        )

    def _cmd_reprise(self, arg: str, author: str) -> str:
        if not self.paused:
            return self._t("L'enregistrement est déjà actif.", "Recording is already active.")
        self.paused = False
        return self._t("Enregistrement du chat repris.", "Chat recording resumed.")

    def _cmd_statut(self, arg: str, author: str) -> str:
        incident = self.store.session_incident(self.session_id)
        count = self.store.count_messages(self.session_id)
        people = len(self.managers) + len(self.allowed)
        if self.language == "en":
            ref = (incident["ref"] or "no reference") if incident else "not started (imv start [ID])"
            access = "open to all" if self.open_to_all else f"restricted ({people} people)"
            text = (
                f"Incident: {ref} · recording: {'paused' if self.paused else 'active'} · "
                f"recorded messages: {count} · commands: {access} · "
                f"analyses: {'on' if self.llm else 'off'}"
                + (f" · search: {self.research_name}" if self.research else "")
            )
        else:
            ref = (incident["ref"] or "sans référence") if incident else "non démarré (imv démarrer [ID])"
            access = "ouvertes à tous" if self.open_to_all else f"réservées ({people} pers.)"
            text = (
                f"Incident : {ref} · enregistrement : {'en pause' if self.paused else 'actif'} · "
                f"messages enregistrés : {count} · commandes : {access} · "
                f"analyses : {'actives' if self.llm else 'non activées'}"
                + (f" · recherche : {self.research_name}" if self.research else "")
            )
        return text if self.sees_all() else f"{text}\n{self._p('blind')}"

    def _cmd_autoriser(self, arg: str, author: str) -> str:
        if not arg.strip():
            return self._t(
                "Précisez qui autoriser : imv autoriser <nom>, ou imv autoriser tous.",
                "Specify who to allow: imv allow <name>, or imv allow all.",
            )
        if _name_key(arg) in _EVERYONE:
            self.open_to_all = True
            return self._t(
                "Commandes IMV ouvertes à tous les participants. « imv retirer tous » pour les réserver de nouveau.",
                "IMV commands open to all participants. “imv revoke all” to restrict them again.",
            )
        name, note = self._resolve_name(arg)
        if name is None:
            return note
        key = _name_key(name)
        if key in self.managers or key in self.allowed:
            return self._t(f"{name} est déjà autorisé.", f"{name} is already allowed.")
        self.allowed[key] = name
        self._refused.discard(key)
        return self._t(f"{name} peut maintenant utiliser les commandes IMV.{note}", f"{name} can now use IMV commands.{note}")

    def _cmd_retirer(self, arg: str, author: str) -> str:
        if not arg.strip():
            return self._t(
                "Précisez qui retirer : imv retirer <nom>, ou imv retirer tous.",
                "Specify who to revoke: imv revoke <name>, or imv revoke all.",
            )
        if _name_key(arg) in _EVERYONE:
            self.open_to_all = False
            self.allowed.clear()
            return self._t(
                f"Commandes IMV de nouveau réservées à {self._managers_label()}.",
                f"IMV commands reserved to {self._managers_label()} again.",
            )
        key = _name_key(arg.strip().lstrip("@"))
        matches = [k for k, n in self.allowed.items() if k == key or all(w in k.split() for w in key.split())]
        if key in self.managers:
            return self._t(
                f"{self.managers[key]} est responsable d'IMV : son accès ne peut pas être retiré.",
                f"{self.managers[key]} is the IMV manager: their access cannot be revoked.",
            )
        if len(matches) > 1:
            return self._t("Plusieurs personnes correspondent : précisez le nom.", "Several people match: please specify the name.")
        if not matches:
            return self._t(f"{arg.strip()} n'est pas dans la liste des autorisés.", f"{arg.strip()} is not in the allowed list.")
        name = self.allowed.pop(matches[0])
        if self.open_to_all:
            return self._t(
                f"{name} ne peut plus utiliser les commandes IMV (les commandes restent ouvertes à tous : "
                "« imv retirer tous » pour les réserver).",
                f"{name} can no longer use IMV commands (commands remain open to all: “imv revoke all” to restrict them).",
            )
        return self._t(f"{name} ne peut plus utiliser les commandes IMV.", f"{name} can no longer use IMV commands.")

    def _cmd_autorises(self, arg: str, author: str) -> str:
        if self.open_to_all:
            return self._t(
                f"Commandes ouvertes à tous. Responsable : {self._managers_label()}.",
                f"Commands open to all. Manager: {self._managers_label()}.",
            )
        others = ", ".join(self.allowed.values()) or self._t("personne d'autre", "nobody else")
        return self._t(
            f"Responsable : {self._managers_label()}. Autorisés : {others}.",
            f"Manager: {self._managers_label()}. Allowed: {others}.",
        )
