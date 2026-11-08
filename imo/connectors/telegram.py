"""Connecteur Telegram : un groupe Telegram tient lieu de chat de réunion, pour essayer Oscar sans Teams.

Un seul bot par serveur (TELEGRAM_BOT_TOKEN), interrogé en « long polling » par TelegramBot, qui
distribue les messages aux sessions groupe par groupe. Lien de « réunion » : telegram:<id du groupe>.
Le bot doit voir tous les messages du groupe : « privacy mode » désactivé (@BotFather, /setprivacy)
ou bot administrateur du groupe.

Les commandes Telegram (« /resume », « /pistes@oscar_bot ») sont traduites en « imv resume » ; une
mention du bot en tête de message vaut l'appel par le nom (« @oscar_bot résumé » → « @Oscar résumé »)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from imo.connectors import JoinError
from imo.models import ChatMessage

log = logging.getLogger(__name__)

URL_PREFIX = "telegram:"
MAX_MESSAGE = 4000  # limite Telegram : 4096 caractères
POLL_TIMEOUT = 10  # secondes d'attente côté Telegram par requête getUpdates
START_COMMAND = "oscar"  # « /oscar » dans un groupe sans session : démarre le suivi
COMMANDS = [  # menu des commandes affiché par Telegram
    ("oscar", "Démarrer le suivi par Oscar"),
    ("resume", "Point de situation"),
    ("pistes", "Hypothèses, vérifications, contournements"),
    ("questions", "Questions à poser maintenant"),
    ("timeline", "Chronologie"),
    ("actions", "Actions et owners"),
    ("rapport", "Aperçu du résumé de fin"),
    ("statut", "État de l'agent"),
    ("aide", "Liste des commandes"),
    ("clore", "Clore l'incident (responsable)"),
]


def is_telegram_url(url: str) -> bool:
    return url.strip().lower().startswith(URL_PREFIX)


def parse_chat_id(url: str) -> int:
    value = url.strip()[len(URL_PREFIX):].strip()
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError(f"lien Telegram invalide : {url!r} (attendu : telegram:<id du groupe>, ex. telegram:-1001234567890)")
    return int(value)


class TelegramAPIError(Exception):
    def __init__(self, status: int, description: str) -> None:
        super().__init__(f"Telegram {status} : {description}")
        self.status = status
        self.description = description


class TelegramAPI:
    """Appels à l'API Bot de Telegram (JSON sur HTTPS), sans dépendance : urllib dans un thread."""

    def __init__(self, token: str, base_url: str | None = None) -> None:
        self.base = (base_url or os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
        self.token = token

    def _call(self, method: str, params: dict, timeout: float) -> object:
        request = urllib.request.Request(
            f"{self.base}/bot{self.token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — adresse fixe
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (json.JSONDecodeError, OSError):
                raise TelegramAPIError(exc.code, exc.reason) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramAPIError(0, f"injoignable ({exc})") from exc
        if not body.get("ok"):
            raise TelegramAPIError(body.get("error_code", 0), body.get("description", "erreur"))
        return body["result"]

    async def call(self, method: str, http_timeout: float = 30, **params) -> object:
        """`params` : paramètres de la méthode Telegram (dont son propre « timeout » pour getUpdates)."""
        return await asyncio.to_thread(self._call, method, params, http_timeout)


def sender_name(user: dict) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or (f"@{user['username']}" if user.get("username") else str(user.get("id", "?")))


def is_allowed(user: dict, allowed: set[str]) -> bool:
    """`allowed` : identifiants numériques ou @noms d'utilisateur Telegram (en minuscules, sans @)."""
    return str(user.get("id")) in allowed or (user.get("username") or "").lower() in allowed


def allowed_users() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    return {u.strip().lstrip("@").lower() for u in raw.split(",") if u.strip()}


class TelegramBot:
    """Le bot du serveur : interroge Telegram et distribue chaque message à la session de son groupe.
    `on_start` : appelé pour « /oscar » dans un groupe sans session (démarrage du suivi)."""

    def __init__(self, token: str, *, poll_timeout: float = POLL_TIMEOUT, base_url: str | None = None) -> None:
        self.api = TelegramAPI(token, base_url)
        self.poll_timeout = poll_timeout
        self.id = 0
        self.username = ""
        self.first_name = ""
        self.reads_all_messages = False
        self.sessions: dict[int, TelegramSession] = {}
        self.chats: dict[int, str] = {}  # groupes vus depuis le démarrage : id → titre
        self.on_start: Callable[[int, str, dict], Awaitable[None]] | None = None
        self.on_chat: Callable[[int, str | None], None] | None = None  # titre None : bot retiré
        self._task: asyncio.Task | None = None
        self._started_at = 0.0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return
            try:
                me = await self.api.call("getMe", http_timeout=15)
            except TelegramAPIError as exc:
                raise JoinError("telegram_unavailable", f"bot Telegram inutilisable : {exc}") from exc
            self.id = me.get("id", 0)
            self.username = me.get("username", "")
            self.first_name = me.get("first_name", "")
            self.reads_all_messages = bool(me.get("can_read_all_group_messages"))
            if not self.reads_all_messages:
                log.warning("Bot Telegram @%s : « privacy mode » actif, il ne voit que les commandes /… "
                            "(à désactiver avec @BotFather, /setprivacy, ou le nommer administrateur)", self.username)
            try:
                await self.api.call("setMyCommands", http_timeout=15, commands=[
                    {"command": c, "description": d} for c, d in COMMANDS])
            except TelegramAPIError as exc:
                log.warning("Menu des commandes Telegram non enregistré : %s", exc)
            self._started_at = time.time()
            self._task = asyncio.create_task(self._poll())
            log.info("Bot Telegram @%s à l'écoute", self.username)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _poll(self) -> None:
        offset, delay = None, 1.0
        while True:
            try:
                params = {"timeout": self.poll_timeout, "allowed_updates": ["message", "my_chat_member"]}
                if offset is not None:
                    params["offset"] = offset
                updates = await self.api.call("getUpdates", http_timeout=self.poll_timeout + 15, **params)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — le bot doit survivre à toute erreur
                # 409 : un autre programme lit ce bot (autre serveur, webhook) ; on réessaie
                log.warning("Telegram : %s ; nouvel essai dans %.0f s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self._dispatch(update)
                except Exception:  # noqa: BLE001 — un message mal formé ne doit pas arrêter le bot
                    log.exception("Message Telegram non traité : %s", update.get("update_id"))

    async def _dispatch(self, update: dict) -> None:
        member = update.get("my_chat_member")
        if member:
            chat = member["chat"]
            status = member.get("new_chat_member", {}).get("status")
            if status in ("left", "kicked"):
                self.chats.pop(chat["id"], None)
                if self.on_chat is not None:
                    self.on_chat(chat["id"], None)
                if (session := self.sessions.get(chat["id"])) is not None:
                    session.end("removed_from_group")
            else:
                self._seen(chat)
            return
        message = update.get("message")
        if not message:
            return
        chat = message["chat"]
        if chat.get("type") not in ("group", "supergroup"):
            if chat.get("type") == "private" and message.get("text"):
                await self._reply(chat["id"], "Ajoutez-moi à un groupe, puis tapez /oscar pour démarrer le suivi.")
            return
        self._seen(chat)
        if "migrate_to_chat_id" in message:  # groupe devenu supergroupe : nouvel identifiant
            if (session := self.sessions.get(chat["id"])) is not None:
                session.end(f"migrated_to:{message['migrate_to_chat_id']}")
            return
        session = self.sessions.get(chat["id"])
        if session is not None:
            session.deliver(message)
            return
        if self._command(message.get("text", "")) == START_COMMAND and message.get("date", 0) >= self._started_at - 5:
            user = message.get("from", {})
            if self.on_start is None:
                return
            await self.on_start(chat["id"], chat.get("title") or str(chat["id"]), user)

    def _seen(self, chat: dict) -> None:
        if chat.get("type") in ("group", "supergroup"):
            title = chat.get("title") or str(chat["id"])
            if self.chats.get(chat["id"]) != title:
                self.chats[chat["id"]] = title
                if self.on_chat is not None:
                    self.on_chat(chat["id"], title)

    def _command(self, text: str) -> str | None:
        """« /resume@oscar_bot arg » → « resume » ; None si ce n'est pas une commande pour ce bot."""
        m = re.match(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?", text.strip())
        if m is None or (m.group(2) and m.group(2).lower() != self.username.lower()):
            return None
        return m.group(1).lower()

    async def _reply(self, chat_id: int, text: str) -> None:
        try:
            await self.api.call("sendMessage", chat_id=chat_id, text=text)
        except TelegramAPIError as exc:
            log.warning("Réponse Telegram non envoyée : %s", exc)


_bots: dict[str, TelegramBot] = {}


def get_bot(token: str | None = None) -> TelegramBot:
    """Bot du processus pour ce jeton (un seul lecteur getUpdates par jeton)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise JoinError("telegram_unavailable", "TELEGRAM_BOT_TOKEN manquant")
    if token not in _bots:
        _bots[token] = TelegramBot(token)
    return _bots[token]


def reset_bots() -> None:
    """Tests : oublie les bots du processus."""
    _bots.clear()


class TelegramSession:
    """Une « réunion » Telegram : un groupe suivi par Oscar. Même interface que TeamsSession pour le
    runner ; pas de sous-titres ni de caméra. Quitter arrête le suivi, le bot reste dans le groupe."""

    def __init__(self, url: str, display_name: str, *, bot: TelegramBot | None = None) -> None:
        self.chat_id = parse_chat_id(url)
        self.url = f"{URL_PREFIX}{self.chat_id}"
        self.display_name = display_name.strip()
        self.debug_dir = None  # attribut attendu par le serveur (captures Teams)
        self.title = str(self.chat_id)
        self._bot = bot
        self._joined_at = 0
        self._on_message: Callable[[ChatMessage], None] | None = None
        self._pending: list[ChatMessage] = []
        self._ended = asyncio.Event()
        self._end_reason = "ended"

    @property
    def bot(self) -> TelegramBot:
        if self._bot is None:
            self._bot = get_bot()
        return self._bot

    async def __aenter__(self) -> TelegramSession:
        await self.bot.start()
        return self

    async def __aexit__(self, *exc) -> None:
        self._unregister()

    # -- connexion --------------------------------------------------------

    async def join(self, *, admission_timeout: float = 0) -> None:
        bot = self.bot
        try:
            chat = await bot.api.call("getChat", http_timeout=15, chat_id=self.chat_id)
        except TelegramAPIError as exc:
            raise JoinError("chat_not_found", f"groupe {self.chat_id} inaccessible au bot (est-il membre ?) : {exc}") from exc
        if chat.get("type") not in ("group", "supergroup"):
            raise JoinError("not_a_group", f"{self.chat_id} n'est pas un groupe Telegram")
        self.title = chat.get("title") or self.title
        current = bot.sessions.get(self.chat_id)
        if current is not None and current is not self and not current._ended.is_set():
            raise JoinError("already_followed", f"le groupe « {self.title} » est déjà suivi par une autre session")
        if not bot.reads_all_messages and not await self._is_admin():
            await self.send_chat(
                "Attention : je ne vois que les commandes /… de ce groupe. Pour suivre la conversation, "
                "désactivez mon « privacy mode » (@BotFather, /setprivacy) ou nommez-moi administrateur."
            )
        self._joined_at = int(time.time())
        bot.sessions[self.chat_id] = self
        log.info("Groupe Telegram « %s » (%s) suivi", self.title, self.chat_id)

    async def _is_admin(self) -> bool:
        try:
            member = await self.bot.api.call("getChatMember", http_timeout=15, chat_id=self.chat_id, user_id=self.bot.id)
        except TelegramAPIError:
            return False
        return member.get("status") in ("administrator", "creator")

    async def open_chat(self) -> None:
        return None

    async def read_chat(self, on_message: Callable[[ChatMessage], None]) -> None:
        self._on_message = on_message
        for message in self._pending:
            on_message(message)
        self._pending.clear()

    async def enable_captions(self) -> bool:
        return False  # pas de voix dans un groupe Telegram

    async def set_spoken_language(self, language: str) -> bool:
        return False

    async def read_captions(self, on_caption: Callable[[ChatMessage], None]) -> None:
        return None

    async def snapshot(self, label: str) -> None:
        return None

    # -- messages ---------------------------------------------------------

    def deliver(self, message: dict) -> None:
        """Message du groupe reçu par le bot."""
        chat_message = self._convert(message)
        if chat_message is None:
            return
        if self._on_message is None:
            self._pending.append(chat_message)
        else:
            self._on_message(chat_message)

    def _convert(self, message: dict) -> ChatMessage | None:
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return None  # autocollant, photo sans légende, événement…
        command = self.bot._command(text)
        if text.startswith("/"):
            if command is None:
                return None  # commande destinée à un autre bot
            rest = re.sub(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?", "", text).strip()
            text = "imv statut" if command == START_COMMAND else f"imv {command} {rest}".strip()
        elif self.bot.username:
            short = self.display_name.split(" - ")[-1].strip() or "IMV"
            text = re.sub(rf"^@{re.escape(self.bot.username)}\b", f"@{short}", text, flags=re.IGNORECASE)
        date = message.get("date", 0)
        return ChatMessage(
            sender=sender_name(message.get("from", {})),
            text=text,
            message_id=str(message.get("message_id")),
            sent_at=datetime.fromtimestamp(date, timezone.utc).isoformat(timespec="seconds") if date else None,
            source="chat",
            backlog=date < self._joined_at,
        )

    async def send_chat(self, text: str) -> bool:
        """Poste `text` dans le groupe (découpé au-delà de 4000 caractères) ; False en cas d'échec."""
        chunks = [text[i:i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)] or [""]
        try:
            for chunk in chunks:
                await self.bot.api.call("sendMessage", http_timeout=30, chat_id=self.chat_id, text=chunk)
        except TelegramAPIError as exc:
            log.warning("Message Telegram non publié : %s", exc)
            return False
        return True

    # -- fin --------------------------------------------------------------

    def end(self, reason: str) -> None:
        self._end_reason = reason
        self._ended.set()

    async def wait_until_ended(self) -> str:
        await self._ended.wait()
        return self._end_reason

    async def leave(self) -> None:
        self._unregister()

    def _unregister(self) -> None:
        if self._bot is not None and self._bot.sessions.get(self.chat_id) is self:
            del self._bot.sessions[self.chat_id]
