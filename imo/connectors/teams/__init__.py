"""Connecteur Teams « participant navigateur » : rejoint une réunion par URL et lit le chat."""

from imo.connectors.teams.join import (
    TeamsJoinError,
    TeamsSession,
    validate_display_name,
)

__all__ = ["TeamsJoinError", "TeamsSession", "validate_display_name"]
