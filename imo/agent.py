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

from imo.analysis import TASKS, UsageError, analyze, contains_secret
from imo.llm import LLM, LLMError
from imo.models import ChatMessage
from imo.sinks import Sink
from imo.storage import Store

log = logging.getLogger(__name__)

WELCOME = (
    "Bonjour, je suis IMV, l'assistant virtuel de l'Incident Manager. "
    "J'enregistre les messages de ce chat sur un serveur interne pour aider au suivi "
    "de l'incident (résumés, questions à poser, rapport).\n"
    "« imv pause » suspend l'enregistrement, « imv aide » liste les commandes."
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
    "imv ? <question> : question libre sur l'incident\n"
    "imv statut : état de l'agent\n"
    "imv pause / imv reprise : suspend / reprend l'enregistrement du chat\n"
    "imv autoriser <nom> | tous : autorise une personne, ou tout le monde, à utiliser IMV (responsable)\n"
    "imv retirer <nom> | tous : retire une autorisation, ou réserve de nouveau IMV aux autorisés (responsable)\n"
    "imv autorisés : personnes autorisées\n"
    "imv aide : cette aide"
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
}
# Commandes ouvertes à tous, et commandes réservées aux responsables.
_PUBLIC = {"aide"}
_MANAGER_ONLY = {"autoriser", "retirer"}
_EVERYONE = {"tous", "tout le monde", "all", "everyone"}
# Commandes prévues plus tard : reconnues, mais pas encore disponibles.
_LATER = {"clore"}
# Orthographe affichée des formes canoniques qui portent un accent.
_DISPLAY = {"demarrer": "démarrer", "resume": "résumé", "ask": "?", "autorises": "autorisés"}

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
    ) -> None:
        self.store = store
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

    async def welcome(self) -> bool:
        text = WELCOME
        if self.managers:
            text += (
                f"\nCommandes réservées à {self._managers_label()}, qui peut autoriser d'autres participants "
                "(« imv autoriser <nom> » ou « imv autoriser tous »)."
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
        if msg.source != "chat":
            return
        self._seen.setdefault(_name_key(msg.sender), msg.sender)
        if msg.backlog:
            return
        command = parse_command(msg.text)
        if command is not None:
            await self.execute(command, msg.sender)
        elif not self.paused and contains_secret(msg.text):
            await self.reply(
                f"Attention : le message de {msg.sender} semble contenir un mot de passe ou un jeton. "
                "Supprimez-le du chat et faites changer ce secret ; partagez-le plutôt par un canal sécurisé. "
                "(Il n'est pas transmis à l'analyse.)",
                kind="proactive",
                rule_id="secret_posted",
            )

    async def execute(self, command: Command, author: str) -> None:
        if not await self._authorize(command, author):
            return
        if command.verb in TASKS:
            if self.llm is None:
                await self.reply("L'analyse n'est pas activée pour cette session (option --llm).")
                return
            task = asyncio.create_task(self._analyze(command))
            self._analyses.add(task)
            task.add_done_callback(self._analyses.discard)
            return
        handler = getattr(self, f"_cmd_{command.verb}", None)
        if handler is not None:
            text = handler(command.arg, author)
        elif command.verb in _LATER:
            text = f"« imv {command.word} » n'est pas encore disponible."
        else:
            text = f"Commande inconnue : « {command.word} »."
            close = difflib.get_close_matches(command.verb, [*_ALIASES, *TASKS, *_LATER], n=1, cutoff=0.6)
            if close:
                verb = _ALIASES.get(close[0], close[0])
                text += f" Vouliez-vous dire « imv {_DISPLAY.get(verb, verb)} » ?"
            text += " Tapez « imv aide » pour la liste."
        await self.reply(text)

    async def _authorize(self, command: Command, author: str) -> bool:
        if command.verb in _PUBLIC:
            return True
        key = _name_key(author)
        if not self.managers:
            self.managers[key] = author
            log.info("Responsable IMV : %s (première commande)", author)
            await self.reply(
                f"{author} est responsable d'IMV (première personne à l'utiliser) : les commandes lui sont "
                "réservées. « imv autoriser <nom> » ou « imv autoriser tous » pour en ouvrir l'accès."
            )
            return True
        if key in self.managers:
            return True
        if command.verb not in _MANAGER_ONLY and (self.open_to_all or key in self.allowed):
            return True
        log.info("Commande « imv %s » refusée à %s", command.word, author)
        if key not in self._refused:
            self._refused.add(key)
            reserved = "aux responsables d'IMV" if command.verb in _MANAGER_ONLY else "aux personnes autorisées"
            await self.reply(
                f"{author}, « imv {command.word} » est réservé {reserved}. "
                f"{self._managers_label()} peut vous ajouter : imv autoriser {author}"
            )
        return False

    def _managers_label(self) -> str:
        return " ou ".join(self.managers.values())

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
            return None, "Plusieurs participants correspondent : " + ", ".join(matches) + ". Précisez le nom."
        return name, " Cette personne n'a pas encore écrit dans le chat : son nom doit être exactement celui affiché par Teams."

    async def _analyze(self, command: Command) -> None:
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
            )
        except UsageError as exc:
            text = str(exc)
        except LLMError as exc:
            log.error("Analyse « %s » impossible : %s", command.word, exc)
            text = f"Analyse « {command.word} » impossible pour le moment ({exc})."
        await self.reply(text or f"Analyse « {command.word} » : réponse vide.")

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
            log.info("IMV → chat : %s", text.splitlines()[0])
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
        return HELP

    def _cmd_demarrer(self, arg: str, author: str) -> str:
        incident = self.store.session_incident(self.session_id)
        if incident is not None:
            return f"Suivi déjà démarré : {incident['ref'] or 'incident sans référence'}."
        ref = arg.split()[0] if arg else None
        self.start_incident(ref, author)
        return (
            f"Suivi de l'incident {ref or '(sans référence)'} démarré, IM : {author}. "
            "Les messages déjà captés y sont rattachés."
        )

    def _cmd_pause(self, arg: str, author: str) -> str:
        if self.paused:
            return "L'enregistrement est déjà en pause. « imv reprise » pour reprendre."
        self.paused = True
        return "Enregistrement du chat en pause. « imv reprise » pour reprendre."

    def _cmd_reprise(self, arg: str, author: str) -> str:
        if not self.paused:
            return "L'enregistrement est déjà actif."
        self.paused = False
        return "Enregistrement du chat repris."

    def _cmd_statut(self, arg: str, author: str) -> str:
        incident = self.store.session_incident(self.session_id)
        ref = (incident["ref"] or "sans référence") if incident else "non démarré (imv démarrer [ID])"
        access = "ouvertes à tous" if self.open_to_all else f"réservées ({len(self.managers) + len(self.allowed)} pers.)"
        return (
            f"Incident : {ref} · enregistrement : {'en pause' if self.paused else 'actif'} · "
            f"messages enregistrés : {self.store.count_messages(self.session_id)} · commandes : {access}"
        )

    def _cmd_autoriser(self, arg: str, author: str) -> str:
        if not arg.strip():
            return "Précisez qui autoriser : imv autoriser <nom>, ou imv autoriser tous."
        if _name_key(arg) in _EVERYONE:
            self.open_to_all = True
            return "Commandes IMV ouvertes à tous les participants. « imv retirer tous » pour les réserver de nouveau."
        name, note = self._resolve_name(arg)
        if name is None:
            return note
        key = _name_key(name)
        if key in self.managers or key in self.allowed:
            return f"{name} est déjà autorisé."
        self.allowed[key] = name
        self._refused.discard(key)
        return f"{name} peut maintenant utiliser les commandes IMV.{note}"

    def _cmd_retirer(self, arg: str, author: str) -> str:
        if not arg.strip():
            return "Précisez qui retirer : imv retirer <nom>, ou imv retirer tous."
        if _name_key(arg) in _EVERYONE:
            self.open_to_all = False
            self.allowed.clear()
            return f"Commandes IMV de nouveau réservées à {self._managers_label()}."
        key = _name_key(arg.strip().lstrip("@"))
        matches = [k for k, n in self.allowed.items() if k == key or all(w in k.split() for w in key.split())]
        if key in self.managers:
            return f"{self.managers[key]} est responsable d'IMV : son accès ne peut pas être retiré."
        if len(matches) != 1:
            return "Plusieurs personnes correspondent : précisez le nom." if matches else f"{arg.strip()} n'est pas dans la liste des autorisés."
        name = self.allowed.pop(matches[0])
        extra = " (les commandes restent ouvertes à tous : « imv retirer tous » pour les réserver)" if self.open_to_all else ""
        return f"{name} ne peut plus utiliser les commandes IMV{extra}."

    def _cmd_autorises(self, arg: str, author: str) -> str:
        if self.open_to_all:
            return f"Commandes ouvertes à tous. Responsable : {self._managers_label()}."
        others = ", ".join(self.allowed.values()) or "personne d'autre"
        return f"Responsable : {self._managers_label()}. Autorisés : {others}."
