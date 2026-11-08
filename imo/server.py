"""Serveur web (imo_agent.py serve) : lance et suit plusieurs agents IMV en même temps,
chacun dans sa réunion, depuis une page web ou l'API JSON.

Sans --auth : écoute 127.0.0.1, sans authentification, réservé au poste de l'utilisateur.
Avec --auth : connexion par identifiant et mot de passe, droits selon le rôle et les groupes
(voir imo/auth.py) ; à placer derrière un proxy HTTPS pour un accès en ligne."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from imo.auth import (
    COOKIE,
    LOCAL_ADMIN,
    LOGIN_DAYS,
    LoginThrottle,
    User,
    check_password,
    check_role,
    hash_password,
    new_api_token,
    new_token,
    token_hash,
    verify_password,
)
from imo import metrics
from imo.agent import parse_command
from imo.analysis import TASKS, UsageError, analyze, load_context
from imo.connectors import JoinError
from imo.connectors.telegram import TelegramBot, allowed_users, get_bot, is_allowed, sender_name
from imo.llm import PROVIDERS, LLMError, make_llm
from imo.runner import DEFAULT_AGENT_NAME, MeetingConfig, MeetingRunner, resolve_context_path
from imo.storage import Store

log = logging.getLogger("imo")
PAGE = Path(__file__).with_name("server.html")
LOGIN_PAGE = Path(__file__).with_name("login.html")
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
# Empreinte calculée une fois : un identifiant inconnu coûte autant qu'un mauvais mot de passe.
_DUMMY_HASH = hash_password("identifiant inconnu")


class MeetingRequest(BaseModel):
    url: str
    name: str | None = None
    im: list[str] = []
    incident: str | None = None
    captions: bool = True
    # Langue des sous-titres et des messages de l'agent ; None : celle de la réunion, messages en français
    captions_language: Literal["fr", "en"] | None = "en"
    avatar: bool = True
    welcome: bool = True
    llm: str | None = None  # None : celui du serveur (--llm) ; "" : aucune analyse
    group_id: int | None = None  # None : le groupe de l'utilisateur s'il n'en a qu'un


class ChatRequest(BaseModel):
    text: str = Field(max_length=4000)


class LoginRequest(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(max_length=200)
    role: str = "viewer"
    group_ids: list[int] = []


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = Field(default=None, max_length=200)
    group_ids: list[int] | None = None


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CommandRequest(BaseModel):
    # Comme dans le chat, sans « imv » : « résumé », « com direction », « ? cause probable », « clore »…
    command: str = Field(min_length=1, max_length=4000)


class AnalyzeRequest(BaseModel):
    command: str = Field(default="resume", min_length=1, max_length=4000)
    language: Literal["fr", "en"] | None = None  # None : celle de la session


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    days: int | None = Field(default=None, ge=1, le=3650)  # None : sans expiration
    user_id: int | None = None  # administrateurs : jeton pour un autre utilisateur


class Meetings:
    """Agents lancés par ce serveur, chacun dans sa tâche asyncio."""

    def __init__(self, store: Store, defaults: MeetingConfig) -> None:
        self.store = store
        self.defaults = defaults  # url ignorée : LLM, contexte, navigateur, captures…
        self.runners: dict[int, MeetingRunner] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    def start(self, req: MeetingRequest, group_id: int | None = None, user: str | None = None) -> MeetingRunner:
        managers = [n.strip() for n in req.im if n.strip()] or [
            n for n in [os.environ.get("IMV_IM", "").strip()] if n
        ]
        config = replace(
            self.defaults,
            group_id=group_id,
            created_by=user,
            url=req.url,
            name=(req.name or "").strip() or None,
            incident=(req.incident or "").strip() or None,
            managers=managers,
            welcome=req.welcome,
            captions=req.captions,
            captions_language=req.captions_language,
            avatar="robot" if req.avatar else None,
            llm=self.defaults.llm if req.llm is None else (req.llm or None),
        )
        if config.llm != self.defaults.llm:
            config.llm_model = None  # le modèle de --llm-model ne vaut que pour le fournisseur du serveur
        return self._launch(config)

    def restart(self, session_id: int, user: str | None = None) -> MeetingRunner:
        """Nouvelle session avec le lien, le nom, le groupe et les options d'une session passée."""
        row = self.store.session(session_id)
        options = self.store.session_options(session_id) or {}  # sessions anciennes : lien et nom seulement
        known = {"incident", "managers", "welcome", "captions", "captions_language", "avatar", "llm", "llm_model"}
        config = replace(
            self.defaults,
            group_id=row["group_id"],
            created_by=user,
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
        "group_id": row["group_id"],
        "group": row["group_name"],
        "created_by": row["created_by"],
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




def create_app(
    db: Path | str,
    defaults: MeetingConfig,
    allowed_hosts: list[str] = LOCAL_HOSTS,
    auth: bool = False,
) -> FastAPI:
    meetings: Meetings | None = None
    throttle = LoginThrottle()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal meetings
        # Base ouverte dans la boucle du serveur : SQLite refuse d'être utilisé depuis un autre thread.
        store = Store(db)
        meetings = Meetings(store, defaults)
        bot = await start_telegram(store)
        try:
            yield
            await meetings.shutdown()
        finally:
            if bot is not None:
                await bot.stop()
            store.close()

    async def start_telegram(store: Store) -> TelegramBot | None:
        """Bot Telegram du serveur (TELEGRAM_BOT_TOKEN) : groupes vus, « /oscar » pour démarrer."""
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            return None
        bot = get_bot()

        def remember(chat_id: int, title: str | None) -> None:
            if title:
                store.save_telegram_chat(chat_id, title)
            else:
                store.forget_telegram_chat(chat_id)

        async def start_from_group(chat_id: int, title: str, user: dict) -> None:
            if not is_allowed(user, allowed_users()):
                log.warning("Telegram : /oscar refusé à %s (id %s) dans « %s »", sender_name(user),
                            user.get("id"), title)
                await bot.api.call("sendMessage", chat_id=chat_id, text=(
                    f"{sender_name(user)}, vous n'êtes pas autorisé à lancer Oscar "
                    f"(identifiant Telegram : {user.get('id')}, à ajouter à TELEGRAM_ALLOWED_USERS)."))
                return
            group = os.environ.get("IMV_TELEGRAM_GROUP", "").strip()
            row = meetings.store.group_by_name(group) if group else None
            language = os.environ.get("IMV_TELEGRAM_LANGUAGE", "fr").strip()
            request = MeetingRequest(
                url=f"telegram:{chat_id}", im=[sender_name(user)], captions=False, avatar=False,
                captions_language=language if language in ("fr", "en") else None,
            )
            who = f"telegram:@{user['username']}" if user.get("username") else f"telegram:{user.get('id')}"
            try:
                runner = meetings.start(request, row["id"] if row else None, who)
            except (ValueError, LLMError) as exc:
                await bot.api.call("sendMessage", chat_id=chat_id, text=f"Impossible de démarrer Oscar : {exc}")
                return
            log.info("Telegram : session %s démarrée par %s dans « %s »", runner.session_id, who, title)

        bot.on_chat = remember
        bot.on_start = start_from_group
        try:
            await bot.start()
        except JoinError as exc:
            log.error("Telegram indisponible : %s", exc)
            return None
        return bot

    # Documentation : /api/docs après connexion (pas les routes publiques par défaut de FastAPI)
    app = FastAPI(title="IMV", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Contre le « DNS rebinding » : un site web ne peut pas joindre ce serveur sous un autre nom.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Contre les requêtes envoyées par un autre site avec le cookie de l'utilisateur
        # (en plus du cookie SameSite=Strict et du corps JSON obligatoire).
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != "null":
            if urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "origine refusée"}, status_code=403)
        return await call_next(request)

    # -- utilisateur courant et droits ---------------------------------------

    bearer = HTTPBearer(auto_error=False, description="Jeton d'API : imo_agent.py token add, ou POST /api/tokens")

    async def optional_user(
        request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
    ) -> User | None:
        """Jeton d'API (scripts), sinon cookie de la page web."""
        request.state.api_token = None
        if not auth:
            return LOCAL_ADMIN
        if credentials is not None:
            found = meetings.store.api_token_user(token_hash(credentials.credentials))
            if found is None:
                raise HTTPException(401, "jeton d'API inconnu ou expiré", headers={"WWW-Authenticate": "Bearer"})
            user, request.state.api_token = found
            return user
        token = request.cookies.get(COOKIE)
        return meetings.store.login_user(token_hash(token)) if token else None

    async def current_user(user: User | None = Depends(optional_user)) -> User:
        if user is None:
            raise HTTPException(401, "connexion requise")
        return user

    async def operator(user: User = Depends(current_user)) -> User:
        if not user.can_operate:
            raise HTTPException(403, "droits insuffisants : rôle lecteur")
        return user

    async def admin(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "réservé aux administrateurs")
        return user

    def visible_session(session_id: int, user: User):
        """Session de la base, si l'utilisateur la voit ; 404 sinon (sans dire qu'elle existe)."""
        row = meetings.store.session(session_id)
        if row is None or not user.sees(row["group_id"]):
            raise HTTPException(404, f"session {session_id} inconnue")
        return row

    def describe_id(session_id: int) -> dict:
        row = meetings.store.session(session_id)
        data = describe(row, meetings.runners.get(session_id), meetings.tasks.get(session_id))
        data["messages"] = meetings.store.count_messages(session_id)
        # Sessions créées avant l'enregistrement des options : relance impossible à l'identique
        data["restartable"] = meetings.store.session_options(session_id) is not None
        return data

    def live_runner(session_id: int, user: User) -> MeetingRunner:
        visible_session(session_id, user)
        runner, task = meetings.runners.get(session_id), meetings.tasks.get(session_id)
        if runner is None or task is None or task.done():
            raise HTTPException(409, "l'agent de cette session n'est plus en réunion")
        return runner

    def meeting_group(requested: int | None, user: User) -> int | None:
        """Groupe d'une nouvelle réunion : celui demandé, s'il est permis, sinon le seul de l'utilisateur."""
        if requested is None:
            if user.is_admin:
                return None if len(user.groups) != 1 else next(iter(user.groups))
            if len(user.groups) == 1:
                return next(iter(user.groups))
            raise HTTPException(400, "choisissez le groupe de la réunion" if user.groups
                                else "vous n'appartenez à aucun groupe : demandez-le à un administrateur")
        if meetings.store.group(requested) is None or not user.sees(requested):
            raise HTTPException(403, "groupe non autorisé")
        return requested

    # -- pages et connexion -------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def page(user: User | None = Depends(optional_user)):
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return PAGE.read_text(encoding="utf-8")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(user: User | None = Depends(optional_user)):
        if user is not None:
            return RedirectResponse("/", status_code=303)
        return LOGIN_PAGE.read_text(encoding="utf-8")

    @app.post("/api/login", tags=["connexion"])
    async def login(req: LoginRequest, request: Request, response: Response) -> dict:
        if not auth:
            raise HTTPException(404, "serveur sans authentification")
        if throttle.blocked(req.username):
            raise HTTPException(429, "trop d'essais : réessayez dans quelques minutes")
        user = meetings.store.user_by_name(req.username.strip())
        stored = meetings.store.password_hash(user.id) if user else None
        if not await asyncio.to_thread(verify_password, req.password, stored or _DUMMY_HASH) or user is None:
            throttle.failed(req.username)
            log.warning("Connexion refusée : %s", req.username)
            raise HTTPException(401, "identifiant ou mot de passe incorrect")
        throttle.succeeded(req.username)
        open_login(user, request, response)
        log.info("Connexion : %s (%s)", user.username, user.role)
        return user.to_dict()

    def open_login(user: User, request: Request, response: Response) -> None:
        token = new_token()
        meetings.store.add_login(token_hash(token), user.id, LOGIN_DAYS)
        response.set_cookie(
            COOKIE,
            token,
            max_age=LOGIN_DAYS * 86400,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",  # https derrière le proxy (en-tête X-Forwarded-Proto)
        )

    @app.post("/api/logout", tags=["connexion"])
    async def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(COOKIE)
        if auth and token:
            meetings.store.delete_login(token_hash(token))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me", tags=["connexion"])
    async def me(user: User = Depends(current_user)) -> dict:
        return {**user.to_dict(), "auth": auth}

    @app.post("/api/me/password", tags=["connexion"])
    async def change_password(req: PasswordChange, request: Request, response: Response,
                              user: User = Depends(current_user)) -> dict:
        if not auth:
            raise HTTPException(404, "serveur sans authentification")
        stored = meetings.store.password_hash(user.id) or ""
        if not await asyncio.to_thread(verify_password, req.current, stored):
            raise HTTPException(400, "mot de passe actuel incorrect")
        try:
            check_password(req.new)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        new_hash = await asyncio.to_thread(hash_password, req.new)
        meetings.store.update_user(user.id, password_hash=new_hash)  # déconnecte partout…
        open_login(user, request, response)  # … sauf ici
        return {"ok": True}

    # -- réunions -----------------------------------------------------------

    @app.get("/api/options", tags=["réunions"])
    async def options(user: User = Depends(current_user)) -> dict:
        context = resolve_context_path(defaults.context)
        groups = (
            [{"id": g["id"], "name": g["name"]} for g in meetings.store.groups()]
            if user.is_admin else user.to_dict()["groups"]
        )
        return {
            "providers": list(PROVIDERS),
            "llm": defaults.llm,
            "context": str(context) if context else None,
            "agent_name": DEFAULT_AGENT_NAME,
            "im": os.environ.get("IMV_IM", "").strip() or None,
            "groups": groups,  # groupes où l'utilisateur peut lancer un agent
            # Groupes Telegram où le bot est présent : lien de réunion « telegram:<id> »
            "telegram": {
                "bot": f"@{get_bot().username}" if os.environ.get("TELEGRAM_BOT_TOKEN") else None,
                "chats": [{"id": r["chat_id"], "title": r["title"]} for r in meetings.store.telegram_chats()],
            },
        }

    @app.get("/api/meetings", tags=["réunions"])
    async def list_meetings(limit: int = 100, user: User = Depends(current_user)) -> list[dict]:
        """Sessions visibles de la base, les plus récentes d'abord (y compris avant un redémarrage, et celles de join)."""
        rows = meetings.store.list_sessions(None if user.is_admin else list(user.groups))[::-1][:limit]
        return [describe(r, meetings.runners.get(r["id"]), meetings.tasks.get(r["id"])) for r in rows]

    @app.post("/api/meetings", status_code=201, tags=["réunions"])
    async def start_meeting(req: MeetingRequest, user: User = Depends(operator)) -> dict:
        # Corps JSON obligatoire (FastAPI refuse le text/plain d'un formulaire d'un autre site :
        # 422) ; DELETE déclenche un contrôle CORS préalable, que ce serveur ne valide jamais.
        group_id = meeting_group(req.group_id, user)
        try:
            runner = meetings.start(req, group_id, user.username if auth else None)
        except (ValueError, LLMError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return describe_id(runner.session_id)

    @app.post("/api/meetings/{session_id}/restart", status_code=201, tags=["réunions"])
    async def restart_meeting(session_id: int, user: User = Depends(operator)) -> dict:
        visible_session(session_id, user)
        task = meetings.tasks.get(session_id)
        if task is not None and not task.done():
            raise HTTPException(409, "l'agent de cette session est encore en réunion")
        try:
            runner = meetings.restart(session_id, user.username if auth else None)
        except (ValueError, LLMError, OSError) as exc:  # OSError : image d'avatar disparue
            raise HTTPException(400, str(exc)) from exc
        return describe_id(runner.session_id)

    @app.delete("/api/meetings/{session_id}", status_code=202, tags=["réunions"])
    async def stop_meeting(session_id: int, user: User = Depends(operator)) -> dict:
        live_runner(session_id, user).stop(farewell=True)
        return describe_id(session_id)

    @app.post("/api/meetings/{session_id}/chat", tags=["réunions"])
    async def send_to_chat(session_id: int, msg: ChatRequest, user: User = Depends(operator)) -> dict:
        """Message tapé dans l'interface web, publié dans le chat de la réunion sous le nom de l'agent."""
        text = msg.text.strip()
        if not text:
            raise HTTPException(400, "message vide")
        delivered = await chatting_agent(session_id, user).reply(text, kind="manual")
        if not delivered:
            raise HTTPException(409, "message non publié : le chat est peut-être fermé aux invités")
        if auth:
            log.info("Session %s : message de %s publié dans le chat", session_id, user.username)
        return {"delivered": True}

    def chatting_agent(session_id: int, user: User):
        runner = live_runner(session_id, user)
        if runner.agent is None or runner.status != "in_meeting" or runner._stop.is_set():
            raise HTTPException(409, "l'agent n'est pas encore (ou plus) dans le chat de la réunion")
        return runner.agent

    @app.post("/api/meetings/{session_id}/command", tags=["réunions"])
    async def run_command(session_id: int, req: CommandRequest, user: User = Depends(operator)) -> dict:
        """Commande de l'agent, comme « imv <commande> » tapé par un responsable dans le chat
        (« résumé », « pistes », « com direction », « ? cause probable », « démarrer INC42 », « pause »,
        « autoriser Sophie », « clore »…). La réponse est publiée dans le chat et renvoyée ; les
        analyses LLM attendent la réponse du modèle. « clore » répond tout de suite, l'agent publie
        ensuite le résumé de fin et quitte la réunion."""
        agent = chatting_agent(session_id, user)
        try:
            command, text, delivered = await agent.api_command(req.command, user.username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"command": command.verb, "arg": command.arg, "text": text, "delivered": delivered}

    @app.post("/api/meetings/{session_id}/analyze", tags=["réunions"])
    async def analyze_meeting(session_id: int, req: AnalyzeRequest, user: User = Depends(operator)) -> dict:
        """Analyse LLM des messages enregistrés (« resume », « timeline », « rapport », « ? question »…),
        renvoyée sans rien publier dans la réunion ; aussi pour une session terminée."""
        row = visible_session(session_id, user)
        command = parse_command(f"imv {req.command}")
        if command is None or command.verb not in TASKS:
            raise HTTPException(400, f"analyse inconnue : {req.command!r} (choix : {', '.join(sorted(TASKS))})")
        options = meetings.store.session_options(session_id) or {}
        runner, task = meetings.runners.get(session_id), meetings.tasks.get(session_id)
        live = runner is not None and task is not None and not task.done()
        try:
            if live and runner.llm is not None:
                llm, context = runner.llm, runner.context
            else:
                name = options.get("llm") or defaults.llm
                if not name:
                    raise HTTPException(400, "aucun LLM pour cette session ni pour le serveur (--llm)")
                model = options.get("llm_model") if options.get("llm") else defaults.llm_model
                llm, context = make_llm(name, model), load_context(resolve_context_path(defaults.context))
            incident = meetings.store.session_incident(session_id)
            text = await analyze(
                llm,
                command.verb,
                meetings.store.messages(session_id),
                incident["ref"] if incident else None,
                command.arg,
                context,
                requester=user.username if auth else None,
                agent_name=row["display_name"],
                language=req.language or options.get("captions_language") or "fr",
            )
        except UsageError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (LLMError, ValueError) as exc:
            raise HTTPException(502, f"analyse impossible : {exc}") from exc
        return {"command": command.verb, "arg": command.arg, "text": text}

    @app.get("/api/metrics", response_class=PlainTextResponse, tags=["supervision"])
    async def prometheus_metrics(_: User = Depends(current_user)) -> PlainTextResponse:
        """Métriques Prometheus (format texte) : agents en cours, sessions, échecs, appels au LLM.
        Avec la connexion obligatoire : jeton d'API d'un utilisateur, même lecteur."""
        live = [r.status for i, r in meetings.runners.items() if not meetings.tasks[i].done()]
        return PlainTextResponse(metrics.render(meetings.store, live), media_type="text/plain; version=0.0.4")

    @app.get("/api/meetings/{session_id}", tags=["réunions"])
    async def get_meeting(session_id: int, user: User = Depends(current_user)) -> dict:
        """État d'une session (à interroger après POST /api/meetings, jusqu'à « in_meeting »)."""
        visible_session(session_id, user)
        return describe_id(session_id)

    @app.get("/api/meetings/{session_id}/feed", tags=["réunions"])
    async def meeting_feed(
        session_id: int, limit: int = Query(300, ge=1, le=10000), user: User = Depends(current_user)
    ) -> dict:
        """État de la session, derniers messages (chat, oral, réponses de l'agent) et journal de l'agent."""
        visible_session(session_id, user)
        runner = meetings.runners.get(session_id)
        return {
            "meeting": describe_id(session_id),
            "feed": feed(meetings.store, session_id, limit),
            "events": list(runner.events) if runner else [],
        }

    # -- jetons d'API ---------------------------------------------------------

    def tokens_enabled() -> None:
        if not auth:
            raise HTTPException(404, "serveur sans authentification : jetons inutiles")

    @app.get("/api/tokens", tags=["jetons d'API"], dependencies=[Depends(tokens_enabled)])
    async def list_tokens(all: bool = False, user: User = Depends(current_user)) -> list[dict]:
        """Jetons de l'utilisateur ; tous les jetons avec all=true (administrateurs)."""
        if all and not user.is_admin:
            raise HTTPException(403, "réservé aux administrateurs")
        return [dict(t) for t in meetings.store.api_tokens(None if all else user.id)]

    @app.post("/api/tokens", status_code=201, tags=["jetons d'API"], dependencies=[Depends(tokens_enabled)])
    async def create_token(req: TokenCreate, request: Request, user: User = Depends(current_user)) -> dict:
        """Crée un jeton d'API, avec les droits de son utilisateur ; le jeton n'est montré qu'ici.
        Depuis la page web uniquement : un jeton ne peut pas en créer d'autres."""
        if request.state.api_token is not None:
            raise HTTPException(403, "un jeton d'API ne peut pas créer de jeton : connectez-vous")
        owner = user
        if req.user_id is not None and req.user_id != user.id:
            if not user.is_admin:
                raise HTTPException(403, "réservé aux administrateurs")
            owner = user_or_404(req.user_id)
        token = new_api_token()
        token_id = meetings.store.add_api_token(owner.id, req.name.strip(), token_hash(token), token[:8], req.days)
        log.info("Jeton d'API « %s » créé pour %s par %s", req.name.strip(), owner.username, user.username)
        return {**dict(next(t for t in meetings.store.api_tokens(owner.id) if t["id"] == token_id)), "token": token}

    @app.delete("/api/tokens/{token_id}", tags=["jetons d'API"], dependencies=[Depends(tokens_enabled)])
    async def delete_token(token_id: int, user: User = Depends(current_user)) -> dict:
        row = meetings.store.api_token(token_id)
        if row is None or (row["user_id"] != user.id and not user.is_admin):
            raise HTTPException(404, f"jeton {token_id} inconnu")
        meetings.store.delete_api_token(token_id)
        log.info("Jeton d'API « %s » révoqué par %s", row["name"], user.username)
        return {"ok": True}

    # -- documentation de l'API (après connexion) -----------------------------

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(_: User = Depends(current_user)) -> dict:
        return app.openapi()

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(user: User | None = Depends(optional_user)):
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="IMV — API")

    # -- administration : utilisateurs et groupes ---------------------------

    def user_or_404(user_id: int) -> User:
        user = meetings.store.user(user_id)
        if user is None:
            raise HTTPException(404, f"utilisateur {user_id} inconnu")
        return user

    def keep_an_admin(target: User, new_role: str | None) -> None:
        if target.is_admin and new_role != "admin" and meetings.store.count_admins() <= 1:
            raise HTTPException(409, "impossible : c'est le dernier administrateur")

    @app.get("/api/users", tags=["administration"])
    async def list_users(_: User = Depends(admin)) -> list[dict]:
        return [u.to_dict() for u in meetings.store.users()]

    @app.post("/api/users", status_code=201, tags=["administration"])
    async def create_user(req: UserCreate, by: User = Depends(admin)) -> dict:
        try:
            check_role(req.role)
            check_password(req.password)
            password_hash = await asyncio.to_thread(hash_password, req.password)
            user_id = meetings.store.create_user(req.username.strip(), password_hash, req.role, req.group_ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("Utilisateur %s créé (%s) par %s", req.username.strip(), req.role, by.username)
        return meetings.store.user(user_id).to_dict()

    @app.patch("/api/users/{user_id}", tags=["administration"])
    async def update_user(user_id: int, req: UserUpdate, by: User = Depends(admin)) -> dict:
        target = user_or_404(user_id)
        try:
            if req.role is not None:
                check_role(req.role)
                keep_an_admin(target, req.role)
            password_hash = None
            if req.password is not None:
                check_password(req.password)
                password_hash = await asyncio.to_thread(hash_password, req.password)
            meetings.store.update_user(user_id, role=req.role, password_hash=password_hash, group_ids=req.group_ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("Utilisateur %s modifié par %s", target.username, by.username)
        return meetings.store.user(user_id).to_dict()

    @app.delete("/api/users/{user_id}", tags=["administration"])
    async def delete_user(user_id: int, by: User = Depends(admin)) -> dict:
        target = user_or_404(user_id)
        if target.id == by.id:
            raise HTTPException(409, "impossible de supprimer son propre compte")
        keep_an_admin(target, None)
        meetings.store.delete_user(user_id)
        log.info("Utilisateur %s supprimé par %s", target.username, by.username)
        return {"ok": True}

    @app.get("/api/groups", tags=["administration"])
    async def list_groups(_: User = Depends(admin)) -> list[dict]:
        return [dict(g) for g in meetings.store.groups()]

    @app.post("/api/groups", status_code=201, tags=["administration"])
    async def create_group(req: GroupCreate, by: User = Depends(admin)) -> dict:
        try:
            group_id = meetings.store.create_group(req.name.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("Groupe %s créé par %s", req.name.strip(), by.username)
        return dict(meetings.store.group(group_id))

    @app.delete("/api/groups/{group_id}", tags=["administration"])
    async def delete_group(group_id: int, by: User = Depends(admin)) -> dict:
        group = meetings.store.group(group_id)
        if group is None:
            raise HTTPException(404, f"groupe {group_id} inconnu")
        meetings.store.delete_group(group_id)
        log.info("Groupe %s supprimé par %s", group["name"], by.username)
        return {"ok": True}

    return app
