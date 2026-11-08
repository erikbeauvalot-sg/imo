"""Participation de l'agent à une réunion, du lancement du navigateur jusqu'à la fin : partagée
par la ligne de commande (join, une réunion) et le serveur web (serve, plusieurs à la fois)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
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

# Le préfixe « IMV - » signale un robot. « Oscar » (alphabet radio) se dit et se transcrit de la
# même façon en français et en anglais : c'est aussi le mot qui interpelle l'agent.
DEFAULT_AGENT_NAME = "IMV - Oscar"
DEFAULT_CONTEXT = Path("contexte.md")


def resolve_context_path(path: Path | None) -> Path | None:
    """--context ou $IMV_CONTEXT, sinon contexte.md du répertoire courant s'il existe."""
    if path is not None:
        return path
    return DEFAULT_CONTEXT if DEFAULT_CONTEXT.is_file() else None


@dataclass
class MeetingConfig:
    url: str
    name: str | None = None  # None : DEFAULT_AGENT_NAME
    incident: str | None = None
    managers: list[str] = field(default_factory=list)
    welcome: bool = True
    captions: bool = False
    # « fr » ou « en » : langue parlée des sous-titres et langue des messages de l'agent ;
    # None : sous-titres dans la langue de la réunion, messages en français.
    captions_language: str | None = None
    avatar: str | None = None  # "robot" ou chemin d'une image ; None : caméra coupée
    llm: str | None = None
    llm_model: str | None = None
    context: Path | None = None
    show: bool = False
    browser: str | None = None
    admission_timeout: float = 600
    debug_dir: Path | None = Path("debug")
    group_id: int | None = None  # groupe propriétaire de la réunion (serveur web)
    created_by: str | None = None  # utilisateur du serveur web qui a lancé l'agent


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
        self.name = config.name or DEFAULT_AGENT_NAME
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
        # Agent Hermes pour « imv hermes <tâche> » : disponible dès que HERMES_API_KEY est défini,
        # quel que soit le LLM des analyses ; client distinct, qui peut se servir des outils.
        self.hermes = make_llm("hermes", hermes_tasks=True) if os.environ.get("HERMES_API_KEY") else None
        self.context_path = resolve_context_path(config.context)
        self.context = load_context(self.context_path) if self.llm or self.hermes else None
        # Options gardées en base : la session peut être relancée à l'identique depuis l'interface web.
        options = {
            "incident": config.incident,
            "managers": config.managers,
            "welcome": config.welcome,
            "captions": config.captions,
            "captions_language": config.captions_language,
            "avatar": config.avatar,
            "llm": config.llm,
            "llm_model": config.llm_model,
        }
        self.session_id = store.create_session(
            self.session.url, self.session.display_name, options, config.group_id, config.created_by
        )
        self.status = "joining"  # joining | in_meeting | ended | failed (comme la base)
        self.error: str | None = None
        self.events: deque[str] = deque(maxlen=200)  # journal propre à cette réunion
        self.agent: IncidentAgent | None = None
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None  # code enregistré en base (ex. server_shutdown)
        self._farewell = False  # message d'au revoir avant de quitter

    def stop(self, reason: str | None = None, *, farewell: bool = False) -> None:
        """Quitte la réunion (si elle est rejointe) et termine run() ; `farewell` : prévient le chat avant."""
        self._stop_reason = self._stop_reason or reason
        self._farewell = self._farewell or farewell
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
            self._set_status("ended", self._stop_reason or "cancelled")
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
                self._set_status("ended", self._stop_reason)
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
                hermes=self.hermes,
                context=self.context,
                managers=config.managers,
                on_close=self.stop,  # « imv clore » : quitte la réunion une fois le résumé publié
                language=config.captions_language or "fr",
            )
            if config.incident:
                agent.start_incident(config.incident)
            queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
            # Lecteur installé avant l'accueil : les messages déjà présents sont marqués « backlog ».
            await session.read_chat(queue.put_nowait)
            # Sous-titres activés avant l'accueil, qui annonce alors la transcription de la voix.
            if config.captions and await session.enable_captions():
                if config.captions_language:
                    await session.set_spoken_language(config.captions_language)
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
            try:
                done, pending = await asyncio.wait({ended, stopped}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # Tâche annulée (arrêt brutal) : quitter quand même, sinon Teams garde un participant
                # fantôme plusieurs minutes après la fermeture du navigateur.
                await _quietly(session.leave(), 5)
                raise
            for task in (*pending, *snapshot_tasks):
                task.cancel()
            if ended in done:
                log.info("Fin de session : %s", ended.result())
                await _quietly(session.snapshot("chat-end"), 10)
            else:
                if self._farewell and not agent.closing:  # « imv clore » a déjà dit au revoir
                    await _quietly(agent.farewell(), 10)
                # Quitter d'abord : la capture de diagnostic (lente sur un vrai Teams) ne doit pas
                # retarder le départ, ni l'empêcher si l'arrêt du serveur est pressé.
                await _quietly(session.snapshot("chat-end"), 5)
                await session.leave()
            worker.cancel()
            while not queue.empty():
                await agent.handle(queue.get_nowait())
            self._set_status("ended", self._stop_reason)
            log.info("Messages enregistrés : %s (session %s)", store.count_messages(self.session_id), self.session_id)
        return 0

    async def _join(self) -> None:
        await self.session.join(admission_timeout=self.config.admission_timeout)
        self._set_status("in_meeting")
        await self.session.open_chat()


async def _quietly(coro, timeout: float) -> None:
    """Étape facultative de fin de session : bornée dans le temps, erreurs ignorées."""
    try:
        await asyncio.wait_for(coro, timeout)
    except Exception:  # noqa: BLE001 — y compris TimeoutError
        pass


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
