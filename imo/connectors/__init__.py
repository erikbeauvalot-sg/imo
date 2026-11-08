"""Connecteurs de réunion : Teams (navigateur) et Telegram (bot), même interface pour le runner."""


class JoinError(Exception):
    """Échec de connexion à la réunion, avec un code exploitable (enregistré en base)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
