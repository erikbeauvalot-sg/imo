"""Ligne de commande : imo_agent.py join <url> | history [session]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from imo.agent import IncidentAgent
from imo.analysis import TASKS, analyze
from imo.connectors.teams import TeamsJoinError, TeamsSession
from imo.llm import PROVIDERS, LLMError, make_llm
from imo.llm.providers import DEFAULT_MODELS
from imo.models import ChatMessage
from imo.sinks import ConsoleSink, JsonlSink, MultiSink
from imo.storage import Store

DEFAULT_NAME = "IMV - Assistant IM"
DEFAULT_DB = Path("sessions") / "imv.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imo_agent.py", description="Incident Manager Virtuel")
    sub = parser.add_subparsers(dest="command", required=True)

    join = sub.add_parser("join", help="Rejoindre une réunion Teams et lire le chat")
    join.add_argument("url", help="URL de la réunion Teams")
    join.add_argument("--name", default=DEFAULT_NAME, help=f"Nom affiché (défaut : {DEFAULT_NAME!r})")
    join.add_argument("--incident", help="Référence de l'incident (sinon : « imv démarrer <ID> » dans le chat)")
    join.add_argument("--no-welcome", action="store_true", help="Ne pas poster le message d'accueil")
    _add_llm_arguments(join, required=False)
    join.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Base SQLite (défaut : {DEFAULT_DB})")
    join.add_argument("--out", type=Path, help="Copie des messages dans un fichier JSONL")
    join.add_argument("--headless", action="store_true", help="Navigateur invisible")
    join.add_argument("--admission-timeout", type=float, default=600, help="Attente max. en salle d'attente (s)")
    join.add_argument("--debug-dir", type=Path, default=Path("debug"), help="Captures en cas d'échec")
    join.add_argument("-v", "--verbose", action="store_true")

    history = sub.add_parser("history", help="Sessions et messages enregistrés")
    history.add_argument("session", nargs="?", type=int, help="Numéro de session (sinon : liste des sessions)")
    history.add_argument("--db", type=Path, default=DEFAULT_DB)
    history.add_argument("-v", "--verbose", action="store_true")

    analyze_cmd = sub.add_parser("analyze", help="Analyser une session enregistrée (sans réunion)")
    analyze_cmd.add_argument("session", type=int, help="Numéro de session (voir « history »)")
    analyze_cmd.add_argument("task", nargs="?", default="resume", choices=sorted(TASKS), help="Analyse (défaut : resume)")
    _add_llm_arguments(analyze_cmd, required=True)
    analyze_cmd.add_argument("--db", type=Path, default=DEFAULT_DB)
    analyze_cmd.add_argument("-v", "--verbose", action="store_true")

    check = sub.add_parser("check-llm", help="Vérifier la connexion au LLM (court message de test)")
    _add_llm_arguments(check, required=True)
    check.add_argument("-v", "--verbose", action="store_true")
    return parser


def _add_llm_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--llm",
        choices=PROVIDERS,
        required=required,
        help="Fournisseur LLM pour les analyses (sans cette option : pas d'analyse)",
    )
    parser.add_argument(
        "--llm-model",
        help="Modèle (défaut : " + ", ".join(f"{p} → {m}" for p, m in DEFAULT_MODELS.items()) + ")",
    )


async def run_join(args: argparse.Namespace) -> int:
    log = logging.getLogger("imo")
    sinks = [ConsoleSink()] + ([JsonlSink(args.out)] if args.out else [])
    session = TeamsSession(args.url, args.name, headless=args.headless, debug_dir=args.debug_dir)
    llm = make_llm(args.llm, args.llm_model) if args.llm else None
    if llm:
        log.info("Analyses avec %s", llm.name)
    store = Store(args.db)
    session_id = store.create_session(session.url, session.display_name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with session:
            try:
                await session.join(admission_timeout=args.admission_timeout)
                store.set_session_status(session_id, "in_meeting")
                await session.open_chat()
            except TeamsJoinError as exc:
                log.error("Connexion impossible — %s", exc)
                store.set_session_status(session_id, "failed", exc.code)
                return 2

            first_message = _FirstMessageSink()
            agent = IncidentAgent(
                store,
                session_id,
                session.display_name,
                session.send_chat,
                sink=MultiSink(*sinks, first_message),
                llm=llm,
            )
            if args.incident:
                agent.start_incident(args.incident)
            queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
            # Lecteur installé avant l'accueil : les messages déjà présents sont marqués « backlog ».
            await session.read_chat(queue.put_nowait)
            if not args.no_welcome:
                await agent.welcome()
            worker = asyncio.create_task(agent.consume(queue))
            snapshot_task = asyncio.create_task(_snapshot_after_first_message(session, first_message))
            log.info("Lecture du chat en cours → %s, session %s (Ctrl+C pour quitter)", args.db, session_id)

            ended = asyncio.create_task(session.wait_until_ended())
            stopped = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait({ended, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for task in (*pending, snapshot_task):
                task.cancel()
            # Prototype : HTML du chat conservé pour ajuster les sélecteurs (contient les messages).
            await session.snapshot("chat-end")
            if ended in done:
                log.info("Fin de session : %s", ended.result())
            else:
                await session.leave()
            worker.cancel()
            while not queue.empty():
                await agent.handle(queue.get_nowait())
            store.set_session_status(session_id, "ended")
            log.info("Messages enregistrés : %s (session %s)", store.count_messages(session_id), session_id)
    finally:
        store.close()
    return 0


class _FirstMessageSink:
    def __init__(self) -> None:
        self.received = asyncio.Event()

    def write(self, message: ChatMessage) -> None:
        if message.source == "chat" and not message.backlog:
            self.received.set()


async def _snapshot_after_first_message(session: TeamsSession, first: _FirstMessageSink) -> None:
    """Prototype : capture du chat peu après le premier message, même si la fenêtre est fermée ensuite."""
    await first.received.wait()
    await asyncio.sleep(3)
    await session.snapshot("chat")


async def run_analyze(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Aucune base : {args.db}", file=sys.stderr)
        return 1
    llm = make_llm(args.llm, args.llm_model)
    store = Store(args.db)
    try:
        messages = store.messages(args.session)
        if not messages:
            print(f"Aucun message pour la session {args.session}", file=sys.stderr)
            return 1
        incident = store.session_incident(args.session)
        print(await analyze(llm, args.task, messages, incident["ref"] if incident else None))
    finally:
        store.close()
    return 0


async def run_check_llm(args: argparse.Namespace) -> int:
    llm = make_llm(args.llm, args.llm_model)
    print(f"Fournisseur : {llm.name}")
    answer = await llm.complete("Tu es un test de connexion.", "Réponds uniquement : OK")
    print(f"Réponse : {answer}")
    return 0


def run_history(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Aucune base : {args.db}", file=sys.stderr)
        return 1
    store = Store(args.db)
    try:
        if args.session is None:
            for s in store.list_sessions():
                status = s["status"] + (f" ({s['error_code']})" if s["error_code"] else "")
                print(
                    f"#{s['id']:<4} {s['created_at']}  {status:<28} "
                    f"incident={s['incident_ref'] or '-':<14} messages={s['message_count']:<5} {s['meeting_url']}"
                )
        else:
            for m in store.messages(args.session):
                print(f"[{m['captured_at'][11:19]}] {m['author_name']} : {m['text']}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Variables d'un fichier .env du répertoire courant ; celles déjà définies sont prioritaires.
    load_dotenv(find_dotenv(usecwd=True))
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command == "history":
        return run_history(args)
    runner = {"join": run_join, "analyze": run_analyze, "check-llm": run_check_llm}.get(args.command)
    if runner is None:
        return 1
    try:
        return asyncio.run(runner(args))
    except (ValueError, LLMError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
