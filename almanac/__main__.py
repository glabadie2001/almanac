import argparse
import logging
import webbrowser
from pathlib import Path

from almanac.config import load
from almanac.door import Service, make_door
from almanac.store import Store
from almanac.web import ensure_token


def main(argv=None):
    parser = argparse.ArgumentParser(description="Almanac: cross-model mission control for Claude & Gemini agents on this PC")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path(".almanac"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true", help="open the signed-in page in the default browser")
    parser.add_argument("--url", action="store_true", help="print the sign-in link and exit")
    parser.add_argument("--seed", type=Path, help="load an inventory call list (tooluses.jsonl) once, then exit")
    parser.add_argument("--ingest", action="store_true", help="ingest the transcripts once, print the result, exit")
    args = parser.parse_args(argv)
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
    namer = Namer(model=settings["naming_model"], claude=settings["claude"],
                  workdir=args.state.resolve() / "namer-cwd") if settings["naming"] else None
    gemini_projects = args.state.resolve() / "gemini" / "projects"
    service = Service(store, settings["transcripts"], settings["interval"],
                      fleet=Fleet(Path(settings["transcripts"]).parent, extra_projects=[gemini_projects]),
                      state=args.state, claude=settings["claude"], gemini=settings["gemini"],
                      prices=Prices(settings["prices"]), namer=namer)
    if args.ingest:
        print(service.ingest())
        return 0
    service.start()
    server = None
    try:
        server = make_door(service, settings["port"], token, hosts=settings["hosts"])
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
