from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ChatMessage:
    """Un message lu dans le chat de la réunion."""

    sender: str
    text: str
    # Identifiant Teams du message (data-mid) quand le DOM l'expose.
    message_id: str | None = None
    # Horodatage affiché par Teams (attribut datetime), sinon None.
    sent_at: str | None = None
    captured_at: str = field(default_factory=utc_now)
    source: str = "chat"  # chat | system (événement Teams) | transcript (V2)
    # Message déjà présent à l'ouverture du chat (avant l'arrivée de l'agent) :
    # enregistré, mais une commande « imv » qu'il contiendrait n'est pas exécutée.
    backlog: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
