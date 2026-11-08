"""Ligne de commande : imo_agent.py join <url> | serve | history [session] | analyze | check-llm | user | group | token."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from imo.analysis import TASKS, analyze, load_context
from imo.auth import ROLE_LABELS, ROLES, check_password, hash_password, new_api_token, token_hash
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

    serve = sub.add_parser("serve", help="Serveur web : lancer et suivre plusieurs agents à la fois")
    serve.add_argument("--port", type=int, default=8765, help="Port (défaut : 8765)")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Adresse d'écoute (défaut : 127.0.0.1 ; derrière un proxy HTTPS, gardez 127.0.0.1)",
    )
    auth = serve.add_mutually_exclusive_group()
    auth.add_argument(
        "--auth",
        action="store_true",
        default=None,
        help="Connexion obligatoire (utilisateurs et groupes : commandes « user » et « group »). "
        "Par défaut : obligatoire dès qu'un utilisateur existe dans la base",
    )
    auth.add_argument(
        "--no-auth",
        dest="auth",
        action="store_false",
        help="Sans connexion, même si des utilisateurs existent (poste local, 127.0.0.1 seulement)",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="NOM",
        help="Nom de domaine sous lequel le serveur est joint, en plus de 127.0.0.1 et localhost (option répétable)",
    )
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

    # --db après l'action (« user add erik --db … ») : options communes à chaque action
    db_option = argparse.ArgumentParser(add_help=False)
    db_option.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Base SQLite (défaut : {DEFAULT_DB})")
    db_option.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)

    user = sub.add_parser("user", help="Utilisateurs du serveur web (connexion obligatoire dès le premier)")
    user_sub = user.add_subparsers(dest="action", required=True)
    user_add = user_sub.add_parser("add", parents=[db_option], help="Créer un utilisateur (mot de passe demandé)")
    user_add.add_argument("username")
    user_add.add_argument("--role", choices=ROLES, default="operator", help="Rôle (défaut : operator)")
    user_add.add_argument("--group", action="append", default=[], metavar="GROUPE",
                          help="Groupe (option répétable ; créé s'il n'existe pas)")
    user_set = user_sub.add_parser("set", parents=[db_option], help="Changer le rôle ou les groupes d'un utilisateur")
    user_set.add_argument("username")
    user_set.add_argument("--role", choices=ROLES)
    user_set.add_argument("--group", action="append", metavar="GROUPE",
                          help="Groupes, qui remplacent les actuels (option répétable ; créés s'ils n'existent pas)")
    user_passwd = user_sub.add_parser(
        "passwd", parents=[db_option], help="Changer le mot de passe d'un utilisateur (le déconnecte)"
    )
    user_passwd.add_argument("username")
    user_sub.add_parser("del", parents=[db_option], help="Supprimer un utilisateur").add_argument("username")
    user_sub.add_parser("list", parents=[db_option], help="Lister les utilisateurs")

    group = sub.add_parser("group", help="Groupes du serveur web : chaque réunion appartient à un groupe")
    group_sub = group.add_subparsers(dest="action", required=True)
    group_sub.add_parser("add", parents=[db_option], help="Créer un groupe").add_argument("name")
    group_sub.add_parser(
        "del", parents=[db_option], help="Supprimer un groupe (ses réunions restent, visibles des admins)"
    ).add_argument("name")
    group_sub.add_parser("list", parents=[db_option], help="Lister les groupes")

    token = sub.add_parser("token", help="Jetons d'API du serveur web (en-tête Authorization: Bearer …)")
    token_sub = token.add_subparsers(dest="action", required=True)
    token_add = token_sub.add_parser(
        "add", parents=[db_option], help="Créer un jeton, avec les droits de l'utilisateur (affiché une seule fois)"
    )
    token_add.add_argument("username")
    token_add.add_argument("--name", required=True, help="Usage du jeton (ex. « supervision »)")
    token_add.add_argument("--days", type=int, help="Validité en jours (défaut : sans expiration)")
    token_list = token_sub.add_parser("list", parents=[db_option], help="Lister les jetons")
    token_list.add_argument("username", nargs="?", help="Seulement ceux de cet utilisateur")
    token_sub.add_parser("del", parents=[db_option], help="Révoquer un jeton").add_argument("id", type=int)

    check = sub.add_parser("check-llm", help="Vérifier la connexion au LLM (court message de test)")
    _add_llm_arguments(check, required=True, context=False)
    check.add_argument("-v", "--verbose", action="store_true")
    return parser


def _add_llm_arguments(parser: argparse.ArgumentParser, *, required: bool, context: bool = True) -> None:
    parser.add_argument(
        "--llm",
        choices=PROVIDERS,
        required=required,
        # argparse ne vérifie pas un défaut : contrôlé dans main()
        default=None if required else (os.environ.get("IMV_LLM") or None),
        help="Fournisseur LLM pour les analyses (défaut : $IMV_LLM ; sans LLM : pas d'analyse)",
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

    from imo.server import LOCAL_HOSTS, create_app

    store = Store(args.db)
    try:
        users, admins = len(store.users()), store.count_admins()
    finally:
        store.close()
    if args.auth is None:
        args.auth = users > 0  # un compte a été créé : on s'attend à devoir se connecter
    if not args.auth and args.host not in LOCAL_HOSTS:
        print(f"Erreur : --host {args.host} expose le serveur au réseau : ajoutez --auth", file=sys.stderr)
        return 1
    if args.auth:
        if not admins:
            print(
                "Erreur : aucun administrateur. Créez-en un : "
                f"python imo_agent.py user add <identifiant> --role admin --db {args.db}",
                file=sys.stderr,
            )
            return 1

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
    app = create_app(args.db, defaults, allowed_hosts=LOCAL_HOSTS + args.allowed_host, auth=args.auth)
    # proxy_headers : derrière le proxy HTTPS local, X-Forwarded-Proto donne le schéma (cookie Secure).
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", proxy_headers=True)
    )
    logging.getLogger("imo").info(
        "IMV : http://%s:%s — %s (Ctrl+C pour arrêter)",
        args.host,
        args.port,
        f"connexion obligatoire ({users} utilisateur(s))" if args.auth
        else "sans connexion, poste local (« user add … --role admin » pour l'activer)",
    )
    await server.serve()
    return 0


def _ask_password() -> str:
    password = getpass.getpass("Mot de passe : ")
    check_password(password)
    if getpass.getpass("Confirmation : ") != password:
        raise ValueError("les deux mots de passe diffèrent")
    return password


def _group_ids(store: Store, names: list[str]) -> list[int]:
    ids = []
    for name in names:
        row = store.group_by_name(name)
        if row is None:
            print(f"Groupe {name} créé")
            ids.append(store.create_group(name))
        else:
            ids.append(row["id"])
    return ids


def run_user(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        if args.action == "list":
            for u in store.users():
                groups = ", ".join(sorted(u.groups.values(), key=str.lower)) or "-"
                print(f"{u.username:<24} {ROLE_LABELS[u.role]:<16} groupes : {groups}")
            return 0
        if args.action == "add":
            store.create_user(args.username, hash_password(_ask_password()), args.role, _group_ids(store, args.group))
            print(f"Utilisateur {args.username} créé ({ROLE_LABELS[args.role]})")
            return 0
        user = store.user_by_name(args.username)
        if user is None:
            raise ValueError(f"utilisateur inconnu : {args.username}")
        if args.action == "del" or (args.action == "set" and args.role not in (None, "admin")):
            if user.is_admin and store.count_admins() <= 1:
                raise ValueError(f"{user.username} est le dernier administrateur")
        if args.action == "set":
            groups = None if args.group is None else _group_ids(store, args.group)
            store.update_user(user.id, role=args.role, group_ids=groups)
        elif args.action == "passwd":
            store.update_user(user.id, password_hash=hash_password(_ask_password()))
        elif args.action == "del":
            store.delete_user(user.id)
        print(f"Utilisateur {user.username} : {'supprimé' if args.action == 'del' else 'modifié'}")
        return 0
    finally:
        store.close()


def run_token(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        if args.action == "del":
            if store.api_token(args.id) is None:
                raise ValueError(f"jeton inconnu : {args.id}")
            store.delete_api_token(args.id)
            print(f"Jeton {args.id} révoqué")
            return 0
        user = store.user_by_name(args.username) if args.username else None
        if args.username and user is None:
            raise ValueError(f"utilisateur inconnu : {args.username}")
        if args.action == "list":
            for t in store.api_tokens(user.id if user else None):
                print(
                    f"#{t['id']:<4} {t['username']:<20} {t['name']:<24} {t['prefix']}…  "
                    f"expire : {t['expires_at'] or 'jamais'}  dernier usage : {t['last_used_at'] or '-'}"
                )
            return 0
        if args.days is not None and args.days < 1:
            raise ValueError("--days doit être positif")
        token = new_api_token()
        store.add_api_token(user.id, args.name, token_hash(token), token[:8], args.days)
        print(f"Jeton « {args.name} » pour {user.username} ({ROLE_LABELS[user.role]}), à conserver : il ne sera plus affiché")
        print(token)
        return 0
    finally:
        store.close()


def run_group(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        if args.action == "list":
            for g in store.groups():
                print(f"{g['name']:<24} membres : {g['members']:<4} réunions : {g['sessions']}")
        elif args.action == "add":
            store.create_group(args.name)
            print(f"Groupe {args.name} créé")
        else:
            row = store.group_by_name(args.name)
            if row is None:
                raise ValueError(f"groupe inconnu : {args.name}")
            store.delete_group(row["id"])
            print(f"Groupe {args.name} supprimé")
        return 0
    finally:
        store.close()


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
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "llm", None) not in (None, *PROVIDERS):
        parser.error(f"IMV_LLM={args.llm} : fournisseur inconnu (choix : {', '.join(PROVIDERS)})")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command == "history":
        return run_history(args)
    if args.command in ("user", "group", "token"):
        try:
            return {"user": run_user, "group": run_group, "token": run_token}[args.command](args)
        except ValueError as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
            return 1
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
