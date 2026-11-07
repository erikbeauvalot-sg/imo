"""Participation de l'agent à une réunion, du lancement du navigateur jusqu'à la fin : partagée
par la ligne de commande (join, une réunion) et le serveur web (serve, plusieurs à la fois)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from imo.agent import IncidentAgent
from imo.analysis import load_context
from imo.connectors.teams import TeamsJoinError, TeamsSession
from imo.connectors.teams.avatar import avatar_video
from imo.llm import make_llm
from imo.models import ChatMessage
from imo.sinks import MultiSink, Sink
from imo.storage import Store

log = logging.getLogger("imo")

# Nom tiré au hasard : plusieurs agents dans une même réunion restent distincts. Le préfixe
# « IMV - » signale un robot ; des noms d'étoiles plutôt que des prénoms de vraies personnes.
AGENT_NAMES = ("Altair", "Atlas", "Castor", "Deneb", "Lyra", "Nova", "Orion", "Rigel", "Sirius", "Vega")
DEFAULT_CONTEXT = Path("contexte.md")


def random_agent_name() -> str:
    return f"IMV - {random.choice(AGENT_NAMES)}"


def resolve_context_path(path: Path | None) -> Path | None:
    """--context ou $IMV_CONTEXT, sinon contexte.md du répertoire courant s'il existe."""
    if path is not None:
        return path
    return DEFAULT_CONTEXT if DEFAULT_CONTEXT.is_file() else None


@dataclass
class MeetingConfig:
    url: str
    name: str | None = None  # None : nom tiré au hasard
    incident: str | None = None
    managers: list[str] = field(default_factory=list)
    welcome: bool = True
    captions: bool = False
    avatar: str | None = None  # "robot" ou chemin d'une image ; None : caméra coupée
    llm: str | None = None
    llm_model: str | None = None
    context: Path | None = None
    show: bool = False
    browser: str | None = None
    admission_timeout: float = 600
    debug_dir: Path | None = Path("debug")


# Réunion en cours dans la tâche asyncio : ses logs lui sont attribués (voir _EventsHandler).
_current: contextvars.ContextVar[MeetingRunner | None] = contextvars.ContextVar("imv_meeting", default=None)


class _EventsHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        runner = _current.get()
        if runner is not None:
            runner.events.append(f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.getMessage()}")


_events_handler = _EventsHandler(logging.INFO)


class MeetingRunner:
    """Valide la configuration dès la construction (URL, nom, LLM, contexte) : ValueError ou
    LLMError avant toute ouverture de navigateur. run() rejoint la réunion et y reste jusqu'à
    sa fin ou à stop()."""

    def __init__(self, config: MeetingConfig, store: Store, sinks: tuple[Sink, ...] = ()) -> None:
        self.config = config
        self.store = store
        self.sinks = sinks
        self.name = config.name or random_agent_name()
        camera_video = None
        if config.avatar:
            camera_video = avatar_video(None if config.avatar == "robot" else Path(config.avatar), label=self.name)
        self.session = TeamsSession(
            config.url,
            self.name,
            headless=not config.show,
            debug_dir=config.debug_dir,
            camera_video=camera_video,
            browser=config.browser,
        )
        self.llm = make_llm(config.llm, config.llm_model) if config.llm else None
        self.context_path = resolve_context_path(config.context)
        self.context = load_context(self.context_path) if self.llm else None
        self.session_id = store.create_session(self.session.url, self.session.display_name)
        self.status = "joining"  # joining | in_meeting | ended | failed (comme la base)
        self.error: str | None = None
        self.events: deque[str] = deque(maxlen=200)  # journal propre à cette réunion
        self.agent: IncidentAgent | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Quitte la réunion (si elle est rejointe) et termine run()."""
        self._stop.set()

    def _set_status(self, status: str, error_code: str | None = None) -> None:
        self.status = status
        self.store.set_session_status(self.session_id, status, error_code)

    async def run(self) -> int:
        """0 : session terminée normalement ; 2 : connexion impossible."""
        _current.set(self)
        if _events_handler not in log.handlers:
            log.addHandler(_events_handler)
        if self.llm:
            log.info("Analyses avec %s%s", self.llm.name, f", contexte : {self.context_path}" if self.context else "")
        try:
            return await self._run()
        except asyncio.CancelledError:
            self._set_status("ended")
            raise
        except Exception as exc:  # noqa: BLE001 — navigateur absent, plantage : la réunion est en échec
            self.error = str(exc)
            log.error("Session interrompue — %s", exc)
            self._set_status("failed", getattr(exc, "code", "error"))
            if isinstance(exc, TeamsJoinError):
                return 2
            raise

    async def _run(self) -> int:
        session, config, store = self.session, self.config, self.store
        async with session:
            join = asyncio.create_task(self._join())
            stopped = asyncio.create_task(self._stop.wait())
            await asyncio.wait({join, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not join.done():  # arrêt demandé en salle d'attente
                join.cancel()
                log.info("Arrêt demandé avant l'admission")
                self._set_status("ended")
                return 0
            stopped.cancel()
            try:
                join.result()
            except TeamsJoinError as exc:
                self.error = str(exc)
                log.error("Connexion impossible — %s", exc)
                if not config.show:
                    log.info("Pour voir ce qui se passe dans le navigateur, relancez avec --show")
                self._set_status("failed", exc.code)
                return 2

            first_message = _FirstMessageSink("chat")
            first_caption = _FirstMessageSink("transcript")
            agent = self.agent = IncidentAgent(
                store,
                self.session_id,
                session.display_name,
                session.send_chat,
                sink=MultiSink(*self.sinks, first_message, first_caption),
                llm=self.llm,
                context=self.context,
                managers=config.managers,
            )
            if config.incident:
                agent.start_incident(config.incident)
            queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
            # Lecteur installé avant l'accueil : les messages déjà présents sont marqués « backlog ».
            await session.read_chat(queue.put_nowait)
            # Sous-titres activés avant l'accueil, qui annonce alors la transcription de la voix.
            if config.captions and await session.enable_captions():
                await session.read_captions(queue.put_nowait)
                agent.transcribing = True
            if config.welcome:
                await agent.welcome()
            worker = asyncio.create_task(agent.consume(queue))
            snapshot_tasks = [
                asyncio.create_task(_snapshot_after_first_message(session, first_message, "chat")),
                asyncio.create_task(_snapshot_after_first_message(session, first_caption, "captions")),
            ]
            log.info("Lecture du chat en cours, session %s", self.session_id)

            ended = asyncio.create_task(session.wait_until_ended())
            stopped = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait({ended, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for task in (*pending, *snapshot_tasks):
                task.cancel()
            # Prototype : HTML du chat conservé pour ajuster les sélecteurs (contient les messages).
            await session.snapshot("chat-end")
            if ended in done:
                log.info("Fin de session : %s", ended.result())
            else:
                await session.leave()
            worker.cancel()
            while not queue.empty():
                await agent.handle(queue.get_nowait())
            self._set_status("ended")
            log.info("Messages enregistrés : %s (session %s)", store.count_messages(self.session_id), self.session_id)
        return 0

    async def _join(self) -> None:
        await self.session.join(admission_timeout=self.config.admission_timeout)
        self._set_status("in_meeting")
        await self.session.open_chat()


class _FirstMessageSink:
    def __init__(self, source: str) -> None:
        self.source = source
        self.received = asyncio.Event()

    def write(self, message: ChatMessage) -> None:
        if message.source == self.source and not message.backlog:
            self.received.set()


async def _snapshot_after_first_message(session: TeamsSession, first: _FirstMessageSink, label: str) -> None:
    """Prototype : capture peu après le premier message (chat ou sous-titre), même si la fenêtre est fermée ensuite."""
    await first.received.wait()
    await asyncio.sleep(3)
    await session.snapshot(label)
