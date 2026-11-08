"""Serveur web local (imo_agent.py serve) : lance et suit plusieurs agents IMV en même temps,
chacun dans sa réunion, depuis une page web ou l'API JSON.

Écoute uniquement 127.0.0.1, sans authentification : réservé au poste de l'utilisateur."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from imo.llm import PROVIDERS, LLMError
from imo.runner import AGENT_NAMES, MeetingConfig, MeetingRunner, resolve_context_path
from imo.storage import Store

log = logging.getLogger("imo")
PAGE = Path(__file__).with_name("server.html")
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


class MeetingRequest(BaseModel):
    url: str
    name: str | None = None
    im: list[str] = []
    incident: str | None = None
    captions: bool = False
    avatar: bool = False
    welcome: bool = True
    llm: str | None = None  # None : celui du serveur (--llm) ; "" : aucune analyse


class ChatRequest(BaseModel):
    text: str = Field(max_length=4000)


class Meetings:
    """Agents lancés par ce serveur, chacun dans sa tâche asyncio."""

    def __init__(self, store: Store, defaults: MeetingConfig) -> None:
        self.store = store
        self.defaults = defaults  # url ignorée : LLM, contexte, navigateur, captures…
        self.runners: dict[int, MeetingRunner] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    def start(self, req: MeetingRequest) -> MeetingRunner:
        managers = [n.strip() for n in req.im if n.strip()] or [
            n for n in [os.environ.get("IMV_IM", "").strip()] if n
        ]
        config = replace(
            self.defaults,
            url=req.url,
            name=(req.name or "").strip() or None,
            incident=(req.incident or "").strip() or None,
            managers=managers,
            welcome=req.welcome,
            captions=req.captions,
            avatar="robot" if req.avatar else None,
            llm=self.defaults.llm if req.llm is None else (req.llm or None),
        )
        if config.llm != self.defaults.llm:
            config.llm_model = None  # le modèle de --llm-model ne vaut que pour le fournisseur du serveur
        return self._launch(config)

    def restart(self, session_id: int) -> MeetingRunner:
        """Nouvelle session avec le lien, le nom et les options d'une session passée."""
        row = self.store.session(session_id)
        options = self.store.session_options(session_id) or {}  # sessions anciennes : lien et nom seulement
        known = {"incident", "managers", "welcome", "captions", "avatar", "llm", "llm_model"}
        config = replace(
            self.defaults,
            url=row["meeting_url"],
            name=row["display_name"],
            **{k: v for k, v in options.items() if k in known},
        )
        return self._launch(config)

    def _launch(self, config: MeetingConfig) -> MeetingRunner:
        runner = MeetingRunner(config, self.store)
        if self.defaults.debug_dir is not None:
            # Captures séparées par réunion : plusieurs agents écrivent en même temps.
            runner.session.debug_dir = self.defaults.debug_dir / f"session-{runner.session_id}"
        self.runners[runner.session_id] = runner
        self.tasks[runner.session_id] = asyncio.create_task(self._run(runner))
        log.info("Session %s : %s rejoint %s", runner.session_id, runner.name, runner.session.url)
        return runner

    async def _run(self, runner: MeetingRunner) -> None:
        try:
            await runner.run()
        except Exception:  # noqa: BLE001 — déjà journalisé et marqué en échec par le runner
            pass

    async def shutdown(self, timeout: float = 30) -> None:
        """Arrêt du serveur : chaque agent quitte sa réunion proprement."""
        for runner in self.runners.values():
            runner.stop("server_shutdown")
        running = [t for t in self.tasks.values() if not t.done()]
        if running:
            log.info("Arrêt de %s agent(s)…", len(running))
            _, pending = await asyncio.wait(running, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def describe(row, runner: MeetingRunner | None, task: asyncio.Task | None) -> dict:
    """Session de la base, complétée par l'état de l'agent s'il tourne dans ce serveur."""
    live = runner is not None and task is not None and not task.done()
    status = runner.status if runner else row["status"]
    if not live and status in ("joining", "in_meeting"):
        status = "interrupted"  # serveur ou commande arrêtés sans clore la session
    agent = runner.agent if live else None
    return {
        "id": row["id"],
        "name": row["display_name"],
        "url": row["meeting_url"],
        "status": status,
        "live": live,
        "stopping": live and runner._stop.is_set(),
        "error": (runner.error if runner else None) or row["error_code"],
        "incident": row["incident_ref"],
        "created_at": row["created_at"],
        "ended_at": row["ended_at"],
        "messages": row["message_count"] if "message_count" in row.keys() else None,
        "llm": runner.llm.name if live and runner.llm else None,
        "captions": live and runner.config.captions,
        "transcribing": bool(agent and agent.transcribing),
        "paused": bool(agent and agent.paused),
        "managers": (sorted(agent.managers.values()) if agent else runner.config.managers) if live else [],
        "open_to_all": bool(agent and agent.open_to_all),
    }


def feed(store: Store, session_id: int, limit: int = 300) -> list[dict]:
    """Messages captés et interventions de l'agent, dans l'ordre chronologique."""
    items = [
        {"time": m["captured_at"], "author": m["author_name"], "text": m["text"], "kind": m["source"]}
        for m in store.messages(session_id)
    ] + [
        {"time": i["timestamp"], "author": "IMV", "text": i["content"],
         "kind": ("manual" if i["type"] == "manual" else "agent") + ("" if i["delivered"] else "-undelivered")}
        for i in store.interventions(session_id)
    ]
    items.sort(key=lambda item: item["time"])
    return items[-limit:]


def create_app(db: Path | str, defaults: MeetingConfig, allowed_hosts: list[str] = LOCAL_HOSTS) -> FastAPI:
    meetings: Meetings | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal meetings
        # Base ouverte dans la boucle du serveur : SQLite refuse d'être utilisé depuis un autre thread.
        store = Store(db)
        meetings = Meetings(store, defaults)
        try:
            yield
            await meetings.shutdown()
        finally:
            store.close()

    app = FastAPI(title="IMV", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Contre le « DNS rebinding » : un site web ne peut pas joindre ce serveur sous un autre nom.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    def session_or_404(session_id: int):
        row = meetings.store.session(session_id)
        if row is None:
            raise HTTPException(404, f"session {session_id} inconnue")
        return row

    def describe_id(session_id: int) -> dict:
        row = session_or_404(session_id)
        data = describe(row, meetings.runners.get(session_id), meetings.tasks.get(session_id))
        data["messages"] = meetings.store.count_messages(session_id)
        # Sessions créées avant l'enregistrement des options : relance impossible à l'identique
        data["restartable"] = meetings.store.session_options(session_id) is not None
        return data

    def live_runner(session_id: int) -> MeetingRunner:
        session_or_404(session_id)
        runner, task = meetings.runners.get(session_id), meetings.tasks.get(session_id)
        if runner is None or task is None or task.done():
            raise HTTPException(409, "l'agent de cette session n'est plus en réunion")
        return runner

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/options")
    async def options() -> dict:
        context = resolve_context_path(defaults.context)
        return {
            "providers": list(PROVIDERS),
            "llm": defaults.llm,
            "context": str(context) if context else None,
            "agent_names": list(AGENT_NAMES),
            "im": os.environ.get("IMV_IM", "").strip() or None,
        }

    @app.get("/api/meetings")
    async def list_meetings(limit: int = 100) -> list[dict]:
        """Sessions de la base, les plus récentes d'abord (y compris avant un redémarrage, et celles de join)."""
        rows = meetings.store.list_sessions()[::-1][:limit]
        return [describe(r, meetings.runners.get(r["id"]), meetings.tasks.get(r["id"])) for r in rows]

    @app.post("/api/meetings", status_code=201)
    async def start_meeting(req: MeetingRequest) -> dict:
        # Corps JSON obligatoire (FastAPI refuse le text/plain d'un formulaire d'un autre site :
        # 422) ; DELETE déclenche un contrôle CORS préalable, que ce serveur ne valide jamais.
        try:
            runner = meetings.start(req)
        except (ValueError, LLMError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return describe_id(runner.session_id)

    @app.post("/api/meetings/{session_id}/restart", status_code=201)
    async def restart_meeting(session_id: int) -> dict:
        session_or_404(session_id)
        task = meetings.tasks.get(session_id)
        if task is not None and not task.done():
            raise HTTPException(409, "l'agent de cette session est encore en réunion")
        try:
            runner = meetings.restart(session_id)
        except (ValueError, LLMError, OSError) as exc:  # OSError : image d'avatar disparue
            raise HTTPException(400, str(exc)) from exc
        return describe_id(runner.session_id)

    @app.delete("/api/meetings/{session_id}", status_code=202)
    async def stop_meeting(session_id: int) -> dict:
        live_runner(session_id).stop()
        return describe_id(session_id)

    @app.post("/api/meetings/{session_id}/chat")
    async def send_to_chat(session_id: int, msg: ChatRequest) -> dict:
        """Message tapé dans l'interface web, publié dans le chat de la réunion sous le nom de l'agent."""
        text = msg.text.strip()
        if not text:
            raise HTTPException(400, "message vide")
        runner = live_runner(session_id)
        if runner.agent is None or runner.status != "in_meeting" or runner._stop.is_set():
            raise HTTPException(409, "l'agent n'est pas encore (ou plus) dans le chat de la réunion")
        delivered = await runner.agent.reply(text, kind="manual")
        if not delivered:
            raise HTTPException(409, "message non publié : le chat est peut-être fermé aux invités")
        return {"delivered": True}

    @app.get("/api/meetings/{session_id}/feed")
    async def meeting_feed(session_id: int) -> dict:
        runner = meetings.runners.get(session_id)
        return {
            "meeting": describe_id(session_id),
            "feed": feed(meetings.store, session_id),
            "events": list(runner.events) if runner else [],
        }

    return app
