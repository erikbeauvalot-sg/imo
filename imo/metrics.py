"""Métriques Prometheus d'IMV, au format texte (sans dépendance) : GET /api/metrics du serveur web.

Compteurs du processus (appels au LLM) et état lu dans la base (sessions, messages, interventions)."""

from __future__ import annotations

import time
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version

START_TIME = time.time()
# (nom du LLM, issue ok|error) → nombre d'appels ; nom du LLM → secondes cumulées
_llm_calls: dict[tuple[str, str], int] = defaultdict(int)
_llm_seconds: dict[str, float] = defaultdict(float)


def record_llm(name: str, seconds: float, ok: bool) -> None:
    _llm_calls[(name, "ok" if ok else "error")] += 1
    _llm_seconds[name] += seconds


def reset() -> None:
    """Tests : remise à zéro des compteurs du processus."""
    _llm_calls.clear()
    _llm_seconds.clear()


def _version() -> str:
    try:
        return version("imo-agent")
    except PackageNotFoundError:
        return "dev"


def _labels(**labels) -> str:
    def escape(value) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in labels.items()) + "}" if labels else ""


class _Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
        self.lines += [f"{name}{_labels(**labels)} {value:g}" for labels, value in samples]

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(store, live_statuses: list[str]) -> str:
    """`live_statuses` : statut de chaque agent en cours dans ce serveur (joining, in_meeting)."""
    out = _Exposition()
    out.metric("imv_info", "gauge", "Version d'IMV.", [({"version": _version()}, 1)])
    out.metric("imv_start_time_seconds", "gauge", "Démarrage du serveur (horodatage Unix).", [({}, START_TIME)])
    out.metric(
        "imv_agents", "gauge", "Agents en cours dans ce serveur, par statut.",
        [({"status": s}, live_statuses.count(s)) for s in ("joining", "in_meeting")],
    )
    db = store.db
    out.metric(
        "imv_sessions", "gauge", "Sessions enregistrées, par statut en base.",
        [({"status": r[0]}, r[1]) for r in db.execute("SELECT status, COUNT(*) FROM sessions GROUP BY status")],
    )
    out.metric(
        "imv_session_failures", "gauge", "Sessions en échec ou arrêtées, par code d'erreur.",
        [({"error_code": r[0]}, r[1]) for r in db.execute(
            "SELECT error_code, COUNT(*) FROM sessions WHERE error_code IS NOT NULL GROUP BY error_code")],
    )
    out.metric(
        "imv_messages", "gauge", "Messages captés (chat, oral, système).",
        [({"source": r[0]}, r[1]) for r in db.execute("SELECT source, COUNT(*) FROM messages GROUP BY source")],
    )
    out.metric(
        "imv_interventions", "gauge", "Messages de l'agent, par type et publication dans le chat.",
        [({"type": r[0], "delivered": "true" if r[1] else "false"}, r[2]) for r in db.execute(
            "SELECT type, delivered, COUNT(*) FROM interventions GROUP BY type, delivered")],
    )
    out.metric(
        "imv_users", "gauge", "Utilisateurs du serveur web, par rôle.",
        [({"role": r[0]}, r[1]) for r in db.execute("SELECT role, COUNT(*) FROM users GROUP BY role")],
    )
    out.metric(
        "imv_llm_requests_total", "counter", "Appels au LLM depuis le démarrage, par issue.",
        [({"llm": name, "outcome": outcome}, n) for (name, outcome), n in sorted(_llm_calls.items())],
    )
    out.metric(
        "imv_llm_request_seconds_total", "counter", "Durée cumulée des appels au LLM depuis le démarrage.",
        [({"llm": name}, seconds) for name, seconds in sorted(_llm_seconds.items())],
    )
    return out.text()
