"""Utilisateurs du serveur web : mots de passe, rôles, groupes et jetons de connexion.

Rôles (du plus large au plus restreint) :
- admin : gère les utilisateurs et les groupes, voit et pilote toutes les réunions ;
- operator : lance, arrête, relance et pilote les agents des réunions de ses groupes ;
- viewer : consulte seulement les réunions de ses groupes.
Une réunion appartient à un groupe ; celles sans groupe (lancées avec join, ou d'avant les groupes)
ne sont visibles que des admins."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

ROLES = ("admin", "operator", "viewer")
ROLE_LABELS = {"admin": "administrateur", "operator": "opérateur", "viewer": "lecteur"}
MIN_PASSWORD = 10
LOGIN_DAYS = 7  # durée de validité d'une connexion
COOKIE = "imv_login"

# scrypt (bibliothèque standard) : ~16 Mo de mémoire et quelques dizaines de ms par essai.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"mot de passe trop court ({MIN_PASSWORD} caractères minimum)")


def check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"rôle inconnu : {role} (attendu : {', '.join(ROLES)})")


def new_token() -> str:
    return secrets.token_urlsafe(32)


API_TOKEN_PREFIX = "imv_"


def new_api_token() -> str:
    """Jeton d'API (en-tête « Authorization: Bearer imv_… ») : préfixe reconnaissable par les
    outils de détection de secrets."""
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Seule l'empreinte du jeton est en base : une copie de la base ne permet pas de se connecter."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    id: int | None
    username: str
    role: str
    groups: dict[int, str] = field(default_factory=dict)  # id → nom

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_operate(self) -> bool:
        return self.role in ("admin", "operator")

    def sees(self, group_id: int | None) -> bool:
        return self.is_admin or (group_id is not None and group_id in self.groups)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "groups": [{"id": i, "name": n} for i, n in sorted(self.groups.items(), key=lambda g: g[1].lower())],
        }


# Serveur sans authentification (poste local) : tous les droits.
LOCAL_ADMIN = User(id=None, username="local", role="admin")


class LoginThrottle:
    """Au-delà de MAX_FAILURES échecs en WINDOW secondes pour un même identifiant, connexion refusée
    jusqu'à ce que les plus anciens sortent de la fenêtre."""

    MAX_FAILURES = 10
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, username: str) -> deque[float]:
        failures = self._failures[username.lower()]
        while failures and failures[0] < time.monotonic() - self.WINDOW:
            failures.popleft()
        return failures

    def blocked(self, username: str) -> bool:
        return len(self._recent(username)) >= self.MAX_FAILURES

    def failed(self, username: str) -> None:
        self._recent(username).append(time.monotonic())

    def succeeded(self, username: str) -> None:
        self._failures.pop(username.lower(), None)
