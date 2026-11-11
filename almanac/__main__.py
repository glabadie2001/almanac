import argparse
import logging
import threading
import webbrowser
from pathlib import Path

from almanac.config import load
from almanac.door import Service, make_door
from almanac.store import Store
from almanac.web import ensure_token


def check(state: Path, modules=None) -> int:
    """The restart preflight: import every module, run the migrations on a copy of the database, build the door's
    handler. 0 and "ok", or the traceback and 1. Never touches the live database."""
    import importlib
    import pkgutil
    import sqlite3
    import tempfile
    import traceback
    import almanac
    from almanac.door import make_handler
    try:
        for name in modules or [f"almanac.{m.name}" for m in pkgutil.iter_modules(almanac.__path__)]:
            importlib.import_module(name)
        with tempfile.TemporaryDirectory() as tmp:
            copy, live = Path(tmp) / "almanac.db", Path(state) / "almanac.db"
            if live.exists():
                src, dst = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro", uri=True), sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
            store = Store(copy)
            try:
                make_handler(Service(store, Path(tmp), 300, state=Path(tmp)), "0" * 64)
            finally:
                store.close()
    except BaseException:
        traceback.print_exc()
        return 1
    print("almanac check: ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Almanac: cross-model mission control for Claude & Gemini agents on this PC")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path(".almanac"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true", help="open the signed-in page in the default browser")
    parser.add_argument("--url", action="store_true", help="print the sign-in link and exit")
    parser.add_argument("--seed", type=Path, help="load an inventory call list (tooluses.jsonl) once, then exit")
    parser.add_argument("--ingest", action="store_true", help="ingest the transcripts once, print the result, exit")
    parser.add_argument("--check", action="store_true",
                        help="restart preflight: import everything, migrate a copy of the database, build the door; exit")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.state)
    settings = load(args.config)
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise SystemExit("port must be 1..65535")
        settings["port"] = args.port
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.state.mkdir(parents=True, exist_ok=True)
    token = ensure_token(args.state / "token")
    login = f"http://127.0.0.1:{settings['port']}/login?token={token}"
    if args.url:
        print(login)
        return 0
    store = Store(args.state / "almanac.db")
    if args.seed:
        print(f"seeded {store.seed_legacy(args.seed)} legacy calls from {args.seed}")
        return 0
    from almanac.fleet import Fleet
    from almanac.namer import Namer
    from almanac.resume import Prices
    from almanac.config import is_shim
    # never name chats through a .cmd shim: cmd.exe would re-parse the command line
    namer = Namer(model=settings["naming_model"], claude=settings["claude"],
                  workdir=args.state.resolve() / "namer-cwd") if settings["naming"] and not is_shim(settings["claude"]) else None
    gemini_projects = args.state.resolve() / "gemini" / "projects"
    service = Service(store, settings["transcripts"], settings["interval"],
                      fleet=Fleet(Path(settings["transcripts"]).parent, extra_projects=[gemini_projects]),
                      state=args.state, claude=settings["claude_configured"], gemini=settings["gemini_configured"],
                      prices=Prices(settings["prices"]), namer=namer)
    service.auto_continue = settings["auto_continue"]
    for kind, runner in service.runners().items():
        if not runner["ok"]:
            logging.warning("%s runner not found; looked in: %s; set \"%s\" in config.json",
                            kind, "; ".join(runner["searched"]), kind)
    if args.ingest:
        print(service.ingest())
        return 0
    service.start()
    server = None
    try:
        server = make_door(service, settings["port"], token, hosts=settings["hosts"])
        # a restart (POST /api/mesh/restart) stops the server from another thread; main then returns its exit code
        service.on_exit = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
        logging.info("Almanac listening on 127.0.0.1:%s, reading %s every %ss", server.server_port,
                     settings["transcripts"], settings["interval"])
        logging.info("Sign in: %s", login)
        if args.open:
            webbrowser.open(login)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop.set()
        service.wake.set()      # the loop sleeps on wake
        service.close_hub()
        if server:
            server.server_close()
    return service.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
