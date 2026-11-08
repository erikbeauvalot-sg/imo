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

from imo.analysis import TASKS, UsageError, analyze, contains_secret, local_hour
from imo.llm import LLM, LLMError
from imo.models import ChatMessage
from imo.sinks import Sink
from imo.storage import Store

log = logging.getLogger(__name__)

# Textes postés dans le chat, dans la langue des sous-titres de la réunion (« fr » ou « en »).
WELCOME = {
    "fr": (
        "Bonjour, je suis {name}, l'assistant virtuel de l'Incident Manager (IMV). "
        "J'enregistre les messages de ce chat sur un serveur interne pour aider au suivi "
        "de l'incident (résumés, questions à poser, rapport).\n"
        "Interpellez-moi par mon nom (« {name}, un résumé ») ou avec « imv » (« imv résumé »). "
        "« imv pause » suspend l'enregistrement, « imv aide » liste les commandes."
    ),
    "en": (
        "Hello, I'm {name}, the Incident Manager's virtual assistant (IMV). "
        "I record the messages of this chat on an internal server to help follow up "
        "on the incident (summaries, questions to ask, report).\n"
        "Call me by my name (“{name}, a summary”) or with “imv” (“imv summary”). "
        "“imv pause” stops recording, “imv help” lists the commands."
    ),
}

HELP_EN = (
    "IMV commands, to type in the chat:\n"
    "imv start [incident ID]: start following the incident\n"
    "imv summary: situation update\n"
    "imv catchup [HH:MM]: short summary for a newcomer, since a given time\n"
    "imv questions: questions to ask now\n"
    "imv leads: hypotheses, checks and workarounds\n"
    "imv timeline: timeline of events\n"
    "imv actions: identified actions and owners\n"
    "imv com [management|users|tech]: communication draft\n"
    "imv report: preview of the final summary\n"
    "imv close: close the incident, post the final summary and leave the meeting (manager)\n"
    "imv ? <question>: free question about the incident\n"
    "imv status: agent status\n"
    "imv pause / imv continue: stop / resume recording the chat\n"
    "imv allow <name> | all: allow a person, or everyone, to use IMV (manager)\n"
    "imv revoke <name> | all: revoke an authorization, or restrict IMV to allowed people again (manager)\n"
    "imv allowed: allowed people\n"
    "imv help: this help\n"
    "Spoken (if the voice is transcribed): “{name}, give me a summary”, “{name}, what are the leads?”…\n"
    "“imv” can be replaced by my name: “{name}, summary”, “{name}, leads”\n"
    "Manager: any other request (“{name}, explain…”, spoken or in the chat) is handled as a free "
    "conversation with the assistant, which takes into account the context and your previous exchanges."
)

HELP = (
    "Commandes IMV, à taper dans le chat :\n"
    "imv démarrer [ID incident] : démarre le suivi de l'incident\n"
    "imv résumé : point de situation\n"
    "imv catchup [HH:MM] : résumé court pour un nouvel arrivant, depuis une heure donnée\n"
    "imv questions : questions à poser maintenant\n"
    "imv pistes : hypothèses, vérifications et contournements\n"
    "imv timeline : chronologie des événements\n"
    "imv actions : actions identifiées et owners\n"
    "imv com [direction|utilisateurs|tech] : brouillon de communication\n"
    "imv rapport : aperçu du résumé de fin\n"
    "imv clore : clôture l'incident, publie le résumé de fin et quitte la réunion (responsable)\n"
    "imv ? <question> : question libre sur l'incident\n"
    "imv statut : état de l'agent\n"
    "imv pause / imv reprise : suspend / reprend l'enregistrement du chat\n"
    "imv autoriser <nom> | tous : autorise une personne, ou tout le monde, à utiliser IMV (responsable)\n"
    "imv retirer <nom> | tous : retire une autorisation, ou réserve de nouveau IMV aux autorisés (responsable)\n"
    "imv autorisés : personnes autorisées\n"
    "imv aide : cette aide\n"
    "À l'oral (si la voix est transcrite) : « IMV, fais un résumé », « IMV, quelles pistes ? »…\n"
    "« imv » peut être remplacé par mon nom : « {name}, résumé », « {name}, quelles pistes ? »\n"
    "Responsable : toute autre demande (« {name}, explique-moi… », à l'oral ou dans le chat) est traitée comme "
    "une conversation libre avec l'assistant, qui tient compte du contexte et de vos échanges précédents."
)

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
_MANAGER_ONLY = {"autoriser", "retirer", "clore", "chat"}
_EVERYONE = {"tous", "tout le monde", "all", "everyone"}
# Commandes prévues plus tard : reconnues, mais pas encore disponibles.
_LATER: set[str] = set()
# Orthographe affichée des formes canoniques qui portent un accent.
_DISPLAY = {"demarrer": "démarrer", "resume": "résumé", "ask": "?", "chat": "?", "autorises": "autorisés"}
_DISPLAY_EN = {
    "aide": "help", "demarrer": "start", "resume": "summary", "pistes": "leads", "rapport": "report",
    "reprise": "continue", "statut": "status", "autoriser": "allow", "retirer": "revoke",
    "autorises": "allowed", "clore": "close", "ask": "?", "chat": "?",
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
     r"|\bou en est", "resume"),
    (r"\bhelp\b|\baide\b", "aide"),
]]
_VOICE_AUDIENCES = [
    (re.compile(r"\b(users?|utilisateurs?|clients?|customers?)\b"), "utilisateurs"),
    (re.compile(r"\b(tech|technical|technique|techniques)\b"), "tech"),
]


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
        context: str | None = None,
        managers: Iterable[str] = (),
        on_close: Callable[[], None] | None = None,
        language: str = "fr",
    ) -> None:
        self.store = store
        # Langue des messages postés et des analyses : celle des sous-titres de la réunion.
        self.language = language
        self.session_id = session_id
        self.display_name = display_name
        self.send = send
        self.sink = sink
        self.llm = llm
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

    async def welcome(self) -> bool:
        text = WELCOME[self._t("fr", "en")].format(name=self.name)
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
        return await self.reply(text, kind="welcome")

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
            await self.execute(self._as_chat(command, msg.sender), msg.sender)
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
        if command.verb in TASKS:
            if self.llm is None:
                await self.reply(self._t(
                    "L'analyse n'est pas activée pour cette session (option --llm).",
                    "Analysis is not enabled for this session (--llm option).",
                ))
                return
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

    def _known(self, verb: str) -> bool:
        return verb in TASKS or verb in _LATER or verb == "clore" or hasattr(self, f"_cmd_{verb}")

    def _as_chat(self, command: Command, author: str) -> Command:
        """« imv <demande libre> » ou « imv ? … » d'un responsable → conversation libre. Une faute de
        frappe sur une commande (« imv resmué », « imv demarer INC1 ») garde sa suggestion."""
        if self.llm is None or _name_key(author) not in self.managers:
            return command
        if command.verb == "ask":
            return Command("chat", "?", command.arg) if command.arg.strip() else command
        if self._known(command.verb):
            return command
        typo = difflib.get_close_matches(command.verb, [*_ALIASES, *TASKS, *_LATER], n=1, cutoff=0.6)
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
            " Cette personne n'a pas encore écrit dans le chat : son nom doit être exactement celui affiché par Teams.",
            " This person hasn't written in the chat yet: their name must be exactly the one shown by Teams.",
        )

    async def _analyze(self, command: Command, author: str) -> None:
        assert self.llm is not None
        incident = self.store.session_incident(self.session_id)
        try:
            text = await analyze(
                self.llm,
                command.verb,
                self.store.messages(self.session_id),
                incident["ref"] if incident else None,
                command.arg,
                self.context,
                requester=author,
                agent_name=self.display_name,
                history=self._chat_history if command.verb == "chat" else (),
                language=self.language,
            )
            if command.verb == "chat" and text:
                self._chat_history.append((author, command.arg, text))
        except UsageError as exc:
            text = str(exc)
        except LLMError as exc:
            log.error("Analyse « %s » impossible : %s", command.word, exc)
            text = self._t(
                f"Analyse « {command.word} » impossible pour le moment ({exc}).",
                f"“{command.word}” analysis unavailable for now ({exc}).",
            )
        await self.reply(text or self._t(f"Analyse « {command.word} » : réponse vide.", f"“{command.word}” analysis: empty answer."))

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
            if key in self.managers:
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
                "résumé de fin, puis je quitterai la réunion.",
                f"{msg.sender}{oral}: “{quote}”. The incident seems over. {ask}: I will post the "
                "final summary, then leave the meeting.",
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
            "Fin du suivi de l'incident par IMV. Je quitte la réunion, merci à tous.",
            "End of incident follow-up by IMV. I'm leaving the meeting, thank you all.",
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
                    language=self.language,
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
        return self._t(HELP, HELP_EN).format(name=self.name)

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
            return (
                f"Incident: {ref} · recording: {'paused' if self.paused else 'active'} · "
                f"recorded messages: {count} · commands: {access}"
            )
        ref = (incident["ref"] or "sans référence") if incident else "non démarré (imv démarrer [ID])"
        access = "ouvertes à tous" if self.open_to_all else f"réservées ({people} pers.)"
        return (
            f"Incident : {ref} · enregistrement : {'en pause' if self.paused else 'actif'} · "
            f"messages enregistrés : {count} · commandes : {access}"
        )

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
