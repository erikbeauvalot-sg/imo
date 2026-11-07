"""Moteur de l'agent pendant le call : enregistrement des messages et commandes « imv »."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from imo.analysis import TASKS, analyze
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
    "imv catchup : résumé court pour un nouvel arrivant\n"
    "imv questions : questions à poser maintenant\n"
    "imv timeline : chronologie des événements\n"
    "imv actions : actions identifiées et owners\n"
    "imv statut : état de l'agent\n"
    "imv pause / imv reprise : suspend / reprend l'enregistrement du chat\n"
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
}
# Commandes prévues plus tard : reconnues, mais pas encore disponibles.
_LATER = {"com", "clore"}
# Orthographe affichée des formes canoniques qui portent un accent.
_DISPLAY = {"demarrer": "démarrer", "resume": "résumé"}

_COMMAND = re.compile(r"^\s*imv\b[\s:,.!-]*(?P<word>\S*)\s*(?P<arg>.*)$", re.IGNORECASE | re.DOTALL)


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


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
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.display_name = display_name
        self.send = send
        self.sink = sink
        self.llm = llm
        self.paused = False
        # Analyses en cours : exécutées en tâche de fond pour ne pas bloquer la lecture du chat.
        self._analyses: set[asyncio.Task] = set()
        # Empreintes des messages envoyés, pour ne pas les relire comme des messages humains.
        self._own: deque[str] = deque(maxlen=50)

    async def welcome(self) -> bool:
        return await self.reply(WELCOME, kind="welcome")

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
        command = parse_command(msg.text) if msg.source == "chat" else None
        if command is not None and not msg.backlog:
            await self.execute(command, msg.sender)

    async def execute(self, command: Command, author: str) -> None:
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

    async def _analyze(self, command: Command) -> None:
        assert self.llm is not None
        incident = self.store.session_incident(self.session_id)
        try:
            text = await analyze(
                self.llm, command.verb, self.store.messages(self.session_id), incident["ref"] if incident else None
            )
        except LLMError as exc:
            log.error("Analyse « %s » impossible : %s", command.word, exc)
            text = f"Analyse « {command.word} » impossible pour le moment ({exc})."
        await self.reply(text or f"Analyse « {command.word} » : réponse vide.")

    async def wait_analyses(self) -> None:
        """Attend la fin des analyses en cours (fin de session, tests)."""
        if self._analyses:
            await asyncio.gather(*self._analyses, return_exceptions=True)

    async def reply(self, text: str, *, kind: str = "command") -> bool:
        fingerprint = _fingerprint(text)
        self._own.append(fingerprint)
        delivered = await self.send(text)
        self.store.add_intervention(self.session_id, kind, text, delivered)
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
        return (
            f"Incident : {ref} · enregistrement : {'en pause' if self.paused else 'actif'} · "
            f"messages enregistrés : {self.store.count_messages(self.session_id)}"
        )
