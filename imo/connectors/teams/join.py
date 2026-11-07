"""Rejoindre une réunion Teams comme invité via un navigateur automatisé, puis lire le chat."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import wave
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from imo.connectors.teams import selectors as sel
from imo.connectors.teams.avatar import media_dir
from imo.models import ChatMessage

log = logging.getLogger(__name__)

CHAT_READER_JS = (Path(__file__).with_name("chat_reader.js")).read_text(encoding="utf-8")

# Teams n'accepte que lettres, chiffres, espaces et - ' . _ @ dans le nom d'un invité,
# sinon le bouton « Join now » reste désactivé.
_DISPLAY_NAME_ALLOWED = re.compile(r"^[\w \-'.@]+$")

# User-agent d'un Chrome classique : Teams peut refuser « HeadlessChrome ».
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


_SYSTEM_MESSAGES = [re.compile(p, re.IGNORECASE) for p in sel.SYSTEM_MESSAGES]


def _sent_at_from_message_id(message_id: str | None) -> str | None:
    # L'identifiant Teams d'un message (data-mid) est son horodatage en millisecondes.
    if message_id and re.fullmatch(r"1\d{12}", message_id):
        return datetime.fromtimestamp(int(message_id) / 1000, timezone.utc).isoformat(timespec="seconds")
    return None


def _blank_media_files() -> tuple[Path, Path]:
    """Fichiers source des faux micro / caméra : 1 s de silence (WAV) et une image noire (Y4M)."""
    folder = media_dir()
    silence, black = folder / "silence.wav", folder / "black.y4m"
    with wave.open(str(silence), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x00" * 48000)
    width, height = 320, 240
    frame = bytes([16]) * (width * height) + bytes([128]) * (width * height // 2)  # YUV 4:2:0 noir
    black.write_bytes(f"YUV4MPEG2 W{width} H{height} F15:1 Ip A1:1 C420jpeg\nFRAME\n".encode() + frame)
    return silence, black


class TeamsJoinError(Exception):
    """Échec de connexion à la réunion, avec un code exploitable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_display_name(name: str) -> str:
    name = name.strip()
    if not name or not _DISPLAY_NAME_ALLOWED.match(name):
        raise ValueError(
            f"Nom d'affichage refusé par Teams : {name!r}. "
            "Caractères autorisés : lettres, chiffres, espaces et - ' . _ @"
        )
    return name


def validate_meeting_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith("." + allowed)
        for allowed in ("teams.microsoft.com", "teams.live.com")
    ):
        raise ValueError(f"URL de réunion Teams invalide : {url!r}")
    return url.strip()


def disable_app_launch(url: str) -> str:
    """URL du lanceur Teams avec msLaunch=false : sinon il ouvre l'application Teams
    installée sur la machine, qui rejoint la réunion avec le compte de l'utilisateur."""
    parsed = urlparse(url)
    if not parsed.path.endswith("/launcher.html"):
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("msLaunch") == "false" and query.get("suppressPrompt") == "true":
        return url
    query.update(msLaunch="false", suppressPrompt="true")
    return urlunparse(parsed._replace(query=urlencode(query)))


class TeamsSession:
    """Une participation de l'agent à une réunion Teams."""

    def __init__(
        self,
        url: str,
        display_name: str,
        *,
        headless: bool = False,
        debug_dir: Path | None = None,
        resolve_launcher: bool = True,
        camera_video: Path | None = None,
        browser: str | None = None,
    ) -> None:
        self.url = validate_meeting_url(url)
        self.display_name = validate_display_name(display_name)
        self.headless = headless
        self.debug_dir = debug_dir
        # Désactivé dans les tests : la résolution fait une requête réseau vers Teams.
        self.resolve_launcher = resolve_launcher
        # Vidéo Y4M diffusée par la caméra (avatar) ; None = caméra coupée.
        self.camera_video = camera_video
        # chromium (téléchargé par « playwright install »), chrome ou msedge (déjà installés) ;
        # None = chromium, sinon Chrome puis Edge s'il n'est pas installé.
        self.browser = browser
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    # -- cycle de vie -----------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        silence, black = _blank_media_files()
        args = [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            # Par défaut, les faux périphériques envoient une mire et un bip :
            # silence et image noire si la caméra ou le micro restaient allumés.
            f"--use-file-for-fake-audio-capture={silence}",
            f"--use-file-for-fake-video-capture={self.camera_video or black}",
            "--disable-blink-features=AutomationControlled",
        ]
        self._browser = await self._launch_browser(args)
        self._context = await self._browser.new_context(
            locale="en-US",
            user_agent=_DESKTOP_UA,
            permissions=["microphone", "camera"],
            viewport={"width": 1366, "height": 900},
        )
        await self._context.route("**/dl/launcher/launcher.html*", self._block_app_launch)
        self.page = await self._context.new_page()

    async def _launch_browser(self, args: list[str]) -> Browser:
        assert self._pw is not None
        candidates = [self.browser] if self.browser else ["chromium", "chrome", "msedge"]
        errors = []
        for name in candidates:
            try:
                # Profil temporaire vierge dans tous les cas, jamais celui de l'utilisateur.
                browser = await self._pw.chromium.launch(
                    headless=self.headless, args=args, channel=None if name == "chromium" else name
                )
            except Exception as exc:  # noqa: BLE001 — navigateur absent : on essaie le suivant
                errors.append(f"{name} : {str(exc).splitlines()[0]}")
                continue
            if name != candidates[0]:
                log.info("Chromium de Playwright absent : utilisation de %s", name)
            return browser
        raise TeamsJoinError(
            "browser_missing",
            "aucun navigateur utilisable. Installez Google Chrome ou Microsoft Edge, "
            "ou lancez « python -m playwright install chromium ». Détails : " + " ; ".join(errors),
        )

    async def _block_app_launch(self, route) -> None:
        request = route.request
        rewritten = disable_app_launch(request.url)
        if rewritten != request.url and request.is_navigation_request():
            log.debug("Lanceur Teams réécrit : msLaunch=false")
            await route.fulfill(status=302, headers={"location": rewritten})
        else:
            await route.fallback()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001 — fermeture best effort
                    pass
        if self._pw is not None:
            await self._pw.stop()

    async def __aenter__(self) -> TeamsSession:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- connexion --------------------------------------------------------

    async def join(self, *, prejoin_timeout: float = 90, admission_timeout: float = 600) -> None:
        page = self._require_page()
        log.info("Ouverture de la réunion…")
        target = await self._resolve_launcher_url() if self.resolve_launcher else self.url
        await page.goto(target, wait_until="domcontentloaded")
        try:
            await self._reach_prejoin(prejoin_timeout)
            await self._fill_prejoin()
            await self._wait_admission(admission_timeout)
        except TeamsJoinError:
            await self.snapshot("join-failed")
            raise

    async def _resolve_launcher_url(self) -> str:
        """Suit la redirection du lien de réunion vers le lanceur et y force msLaunch=false.

        Playwright n'intercepte pas les redirections : la route de start() ne suffit
        pas pour la redirection 302 du lien /meet/... vers launcher.html."""
        assert self._context is not None
        try:
            response = await self._context.request.get(self.url, max_redirects=0, timeout=15000)
            location = response.headers.get("location")
            await response.dispose()
        except Exception as exc:  # noqa: BLE001 — on retombe sur l'URL d'origine
            log.debug("Résolution du lien impossible : %s", exc)
            return self.url
        if not location:
            return self.url
        target = disable_app_launch(urljoin(self.url, location))
        log.debug("Lien résolu : %s", target)
        return target

    async def _reach_prejoin(self, timeout: float) -> None:
        page = self._require_page()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_not_login_page()
            if await self._click_first(sel.CONTINUE_WITHOUT_MEDIA):
                continue
            if await self._visible(sel.JOIN_NOW) or await self._visible(sel.NAME_INPUT):
                log.info("Écran de pré-connexion atteint")
                return
            if await self._click_first(sel.CONTINUE_IN_BROWSER):
                log.info("« Continuer dans ce navigateur » cliqué")
            await page.wait_for_timeout(500)
        raise TeamsJoinError("prejoin_timeout", "écran de pré-connexion introuvable")

    async def _fill_prejoin(self) -> None:
        page = self._require_page()
        name_input = await self._first_visible(sel.NAME_INPUT)
        if name_input is not None:
            await name_input.fill(self.display_name)
        # V1 : pas de micro ; caméra coupée, sauf avatar. « Don't use audio » sera retiré en V2 (écoute).
        await self._click_first(sel.DONT_USE_AUDIO)
        if self.camera_video:
            await self._click_first(sel.CAMERA_OFF)  # bascule : allume la caméra si elle est coupée
        else:
            await self._click_first(sel.CAMERA_ON)
        await self._click_first(sel.MIC_ON)
        await self._click_first(sel.CONTINUE_WITHOUT_MEDIA)
        checks = [(sel.MIC_ON, "micro")] + ([] if self.camera_video else [(sel.CAMERA_ON, "caméra")])
        for selectors, device in checks:
            if await self._visible(selectors):
                log.warning("Impossible de couper le %s : l'agent n'envoie que du noir / du silence", device)

        join_now = await self._first_visible(sel.JOIN_NOW)
        if join_now is None:
            raise TeamsJoinError("join_button_missing", "bouton « Join now » introuvable")
        deadline = time.monotonic() + 10
        while not await join_now.is_enabled():
            if time.monotonic() > deadline:
                raise TeamsJoinError(
                    "prejoin_blocked",
                    "« Join now » reste désactivé (nom refusé ou invités interdits ?)",
                )
            await self._click_first(sel.CONTINUE_WITHOUT_MEDIA)
            await page.wait_for_timeout(300)
        await join_now.click()
        log.info("« Join now » cliqué en tant que %r", self.display_name)

    async def _wait_admission(self, timeout: float) -> None:
        page = self._require_page()
        deadline = time.monotonic() + timeout
        announced_lobby = False
        while time.monotonic() < deadline:
            self._check_not_login_page()
            if await self._visible(sel.IN_MEETING):
                log.info("Admis dans la réunion")
                return
            if await self._visible(sel.REJECTED):
                raise TeamsJoinError("rejected", "l'admission a été refusée")
            if await self._visible(sel.REMOVED_OR_ENDED):
                raise TeamsJoinError("meeting_ended", "la réunion s'est terminée avant l'admission")
            if not announced_lobby and await self._visible(sel.LOBBY):
                log.info("En salle d'attente : un participant doit admettre « %s »", self.display_name)
                announced_lobby = True
            await self._click_first(sel.CONTINUE_WITHOUT_MEDIA)
            await page.wait_for_timeout(1000)
        raise TeamsJoinError("admission_timeout", f"non admis après {timeout:.0f} s")

    # -- chat -------------------------------------------------------------

    async def open_chat(self, timeout: float = 15, input_timeout: float = 10) -> None:
        page = self._require_page()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._click_first(sel.CHAT_BUTTON):
                break
            await page.wait_for_timeout(500)
        else:
            await self.snapshot("chat-button-missing")
            raise TeamsJoinError("chat_unavailable", "bouton du chat introuvable")
        # Le panneau se charge après le clic, parfois en plusieurs secondes (poste lent, Edge) :
        # sans cette attente, le message d'accueil part avant que la zone de saisie existe.
        deadline = time.monotonic() + input_timeout
        while time.monotonic() < deadline:
            if await self._visible(sel.CHAT_INPUT):
                log.info("Panneau de chat ouvert")
                return
            await page.wait_for_timeout(250)
        log.warning("Panneau de chat ouvert, mais sans zone de saisie après %.0f s (chat en lecture seule ?)", input_timeout)

    async def read_chat(self, on_message: Callable[[ChatMessage], None]) -> None:
        """Installe le lecteur de chat ; `on_message` est appelé pour chaque nouveau message."""
        page = self._require_page()

        def _callback(payload: dict) -> None:
            text = str(payload.get("text") or "")
            is_system = any(p.search(text) for p in _SYSTEM_MESSAGES)
            on_message(
                ChatMessage(
                    sender="Teams" if is_system else str(payload.get("sender") or "Inconnu"),
                    text=text,
                    message_id=payload.get("messageId"),
                    sent_at=payload.get("sentAt") or _sent_at_from_message_id(payload.get("messageId")),
                    source="system" if is_system else "chat",
                    backlog=bool(payload.get("backlog")),
                )
            )

        await page.expose_function("__imvOnMessage", _callback)
        await page.evaluate(CHAT_READER_JS)

    async def send_chat(self, text: str) -> bool:
        """Poste `text` dans le chat ; False si la zone de saisie est absente (chat en lecture seule)."""
        page = self._require_page()
        box = await self._first_visible(sel.CHAT_INPUT)
        if box is None:
            return False
        try:
            await box.click(timeout=3000)
            for i, line in enumerate(text.split("\n")):
                if i:
                    await page.keyboard.press("Shift+Enter")
                await page.keyboard.insert_text(line)
            await page.keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            log.warning("Envoi dans le chat impossible : %s", exc)
            return False
        return True

    async def chat_state(self) -> dict:
        return await self._require_page().evaluate(
            "() => window.__imvChatReader ? window.__imvChatReader.state() : null"
        )

    async def wait_until_ended(self, poll_seconds: float = 2) -> str:
        """Bloque jusqu'à la fin de la réunion ou au retrait de l'agent ; renvoie la raison."""
        page = self._require_page()
        while True:
            if page.is_closed():
                return "page_closed"
            if await self._visible(sel.REMOVED_OR_ENDED):
                return "ended_or_removed"
            await asyncio.sleep(poll_seconds)

    async def leave(self) -> None:
        if self.page is None or self.page.is_closed():
            return
        if await self._click_first(sel.HANGUP):
            log.info("Réunion quittée")
            await self.page.wait_for_timeout(1000)

    # -- diagnostic -------------------------------------------------------

    async def snapshot(self, label: str) -> Path | None:
        """Capture d'écran + HTML pour ajuster les sélecteurs sur le vrai Teams."""
        if self.debug_dir is None or self.page is None or self.page.is_closed():
            return None
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stem = self.debug_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
        try:
            await self.page.screenshot(path=f"{stem}.png", full_page=True)
            Path(f"{stem}.html").write_text(await self.page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — le diagnostic ne doit pas masquer l'erreur
            log.warning("Capture de diagnostic impossible : %s", exc)
            return None
        log.info("Diagnostic enregistré : %s.{png,html}", stem)
        return stem

    # -- utilitaires ------------------------------------------------------

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("TeamsSession.start() n'a pas été appelé")
        return self.page

    def _check_not_login_page(self) -> None:
        host = urlparse(self._require_page().url).hostname or ""
        if host in sel.LOGIN_HOSTS:
            raise TeamsJoinError(
                "login_required",
                "Teams redirige vers la connexion Microsoft : les invités anonymes "
                "sont probablement interdits pour cette réunion",
            )

    async def _first_visible(self, selectors: list[str]):
        page = self._require_page()
        for s in selectors:
            loc = page.locator(s).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001 — sélecteur absent ou page en transition
                continue
        return None

    async def _visible(self, selectors: list[str]) -> bool:
        return await self._first_visible(selectors) is not None

    async def _click_first(self, selectors: list[str]) -> bool:
        loc = await self._first_visible(selectors)
        if loc is None:
            return False
        try:
            await loc.click(timeout=3000)
            return True
        except Exception:  # noqa: BLE001
            return False
