"""Ligne de commande : imo_agent.py join <url> | serve | history [session] | analyze | check-llm."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from imo.analysis import TASKS, analyze, load_context
from imo.connectors.teams import TeamsJoinError
from imo.llm import PROVIDERS, LLMError, make_llm
from imo.llm.providers import DEFAULT_MODELS
from imo.runner import DEFAULT_AGENT_NAME, DEFAULT_CONTEXT, MeetingConfig, MeetingRunner, resolve_context_path
from imo.sinks import ConsoleSink, JsonlSink
from imo.storage import Store

DEFAULT_DB = Path("sessions") / "imv.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imo_agent.py", description="Incident Manager Virtuel")
    sub = parser.add_subparsers(dest="command", required=True)

    join = sub.add_parser("join", help="Rejoindre une réunion Teams et lire le chat")
    join.add_argument("url", help="URL de la réunion Teams")
    join.add_argument(
        "--name",
        default=os.environ.get("IMV_NAME") or None,
        help=f"Nom affiché (défaut : $IMV_NAME, sinon « {DEFAULT_AGENT_NAME} ») ; le mot après « IMV - » "
        "sert aussi à interpeller l'agent",
    )
    join.add_argument("--incident", help="Référence de l'incident (sinon : « imv démarrer <ID> » dans le chat)")
    join.add_argument(
        "--im",
        action="append",
        metavar="NOM",
        help="Responsable d'IMV, nom exact affiché par Teams (option répétable ; défaut : $IMV_IM, sinon "
        "la première personne qui tape une commande). Lui seul peut autoriser d'autres participants.",
    )
    join.add_argument("--no-welcome", action="store_true", help="Ne pas poster le message d'accueil")
    join.add_argument(
        "--captions",
        action="store_true",
        help="Transcrire la voix en lisant les sous-titres en direct de Teams (prototype)",
    )
    join.add_argument(
        "--captions-language",
        choices=["fr", "en"],
        help="Langue de la réunion : langue parlée des sous-titres (réglée pour toute la réunion) et langue "
        "des messages de l'agent (défaut : sous-titres inchangés, messages en français)",
    )
    join.add_argument(
        "--avatar",
        nargs="?",
        const="robot",
        metavar="IMAGE",
        help="Caméra allumée avec un avatar : le robot IMV (sans valeur) ou une image PNG/JPEG",
    )
    _add_llm_arguments(join, required=False)
    join.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Base SQLite (défaut : {DEFAULT_DB})")
    join.add_argument("--out", type=Path, help="Copie des messages dans un fichier JSONL")
    join.add_argument("--show", action="store_true", help="Afficher la fenêtre du navigateur (diagnostic)")
    join.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # ancien nom, c'est le défaut
    join.add_argument(
        "--browser",
        choices=("chromium", "chrome", "msedge"),
        default=os.environ.get("IMV_BROWSER") or None,
        help="Navigateur : chromium (playwright install), chrome ou msedge déjà installés "
        "(défaut : $IMV_BROWSER, sinon chromium puis chrome puis msedge)",
    )
    join.add_argument("--admission-timeout", type=float, default=600, help="Attente max. en salle d'attente (s)")
    join.add_argument("--debug-dir", type=Path, default=Path("debug"), help="Captures en cas d'échec")
    join.add_argument("-v", "--verbose", action="store_true")

    serve = sub.add_parser("serve", help="Serveur web local : lancer et suivre plusieurs agents à la fois")
    serve.add_argument("--port", type=int, default=8765, help="Port sur 127.0.0.1 (défaut : 8765)")
    _add_llm_arguments(serve, required=False)
    serve.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Base SQLite (défaut : {DEFAULT_DB})")
    serve.add_argument("--show", action="store_true", help="Afficher les fenêtres des navigateurs (diagnostic)")
    serve.add_argument(
        "--browser", choices=("chromium", "chrome", "msedge"), default=os.environ.get("IMV_BROWSER") or None
    )
    serve.add_argument("--admission-timeout", type=float, default=600, help="Attente max. en salle d'attente (s)")
    serve.add_argument("--debug-dir", type=Path, default=Path("debug"), help="Captures (un sous-dossier par session)")
    serve.add_argument("-v", "--verbose", action="store_true")

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
    _add_llm_arguments(check, required=True, context=False)
    check.add_argument("-v", "--verbose", action="store_true")
    return parser


def _add_llm_arguments(parser: argparse.ArgumentParser, *, required: bool, context: bool = True) -> None:
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
    if not context:
        return
    parser.add_argument(
        "--context",
        type=Path,
        default=os.environ.get("IMV_CONTEXT") or None,
        metavar="FICHIER",
        help="Contexte de l'entreprise pour le LLM : applications, équipes, escalades… "
        f"(défaut : $IMV_CONTEXT, sinon {DEFAULT_CONTEXT} s'il existe ; modèle : contexte.exemple.md)",
    )


async def run_join(args: argparse.Namespace) -> int:
    sinks = (ConsoleSink(),) + ((JsonlSink(args.out),) if args.out else ())
    config = MeetingConfig(
        url=args.url,
        name=args.name,
        incident=args.incident,
        managers=args.im or [n for n in [os.environ.get("IMV_IM", "")] if n.strip()],
        welcome=not args.no_welcome,
        captions=args.captions,
        captions_language=args.captions_language,
        avatar=args.avatar,
        llm=args.llm,
        llm_model=args.llm_model,
        context=args.context,
        show=args.show,
        browser=args.browser,
        admission_timeout=args.admission_timeout,
        debug_dir=args.debug_dir,
    )
    store = Store(args.db)
    try:
        runner = MeetingRunner(config, store, sinks)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runner.stop)
        logging.getLogger("imo").info("Base %s, session %s (Ctrl+C pour quitter)", args.db, runner.session_id)
        return await runner.run()
    finally:
        store.close()


async def run_serve(args: argparse.Namespace) -> int:
    import uvicorn  # importé ici : inutile aux autres commandes

    from imo.server import create_app

    defaults = MeetingConfig(
        url="",
        llm=args.llm,
        llm_model=args.llm_model,
        context=args.context,
        show=args.show,
        browser=args.browser,
        admission_timeout=args.admission_timeout,
        debug_dir=args.debug_dir,
    )
    app = create_app(args.db, defaults)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    logging.getLogger("imo").info("IMV : http://127.0.0.1:%s (Ctrl+C pour arrêter)", args.port)
    await server.serve()
    return 0


async def run_analyze(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Aucune base : {args.db}", file=sys.stderr)
        return 1
    llm = make_llm(args.llm, args.llm_model)
    context = load_context(resolve_context_path(args.context))
    store = Store(args.db)
    try:
        messages = store.messages(args.session)
        if not messages:
            print(f"Aucun message pour la session {args.session}", file=sys.stderr)
            return 1
        incident = store.session_incident(args.session)
        print(await analyze(llm, args.task, messages, incident["ref"] if incident else None, context=context))
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
    runner = {"join": run_join, "serve": run_serve, "analyze": run_analyze, "check-llm": run_check_llm}.get(args.command)
    if runner is None:
        return 1
    try:
        return asyncio.run(runner(args))
    except (ValueError, OSError, LLMError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except TeamsJoinError as exc:  # navigateur introuvable au démarrage
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
