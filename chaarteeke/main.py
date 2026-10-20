"""Command line: serve | seed-demo | reset-demo | preflight | purge-now | create-admin."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from .config import get_settings


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    from .api.server_lock import refusal_to_serve

    s = get_settings()
    host, port = args.host or s.host, args.port or s.port
    app = create_app(
        s.model_copy(update={"host": host, "port": port})
    )  # the QR allow-list reads the real ones
    if line := refusal_to_serve(app.state.ctx):
        print(line, file=sys.stderr)
        return 2
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True)
    return 0


def _seed(args: argparse.Namespace, reset: bool) -> int:
    from .api.app import build_context
    from .api.server_lock import SERVER_RUNNING, foreign, stored_now
    from .demo.controls import KV_OFFSET
    from .demo.seed import SeedError, logins_path, seed

    s = get_settings()
    if not s.is_demo:
        print("seed-demo runs only in CT_MODE=demo", file=sys.stderr)
        return 2
    ctx = build_context(s)
    if foreign(ctx, stored_now(ctx)):  # a running server keeps its jumped clock, and would not see this reset
        print(SERVER_RUNNING, file=sys.stderr)
        ctx.db.close()
        return 2
    try:
        report = seed(ctx, seed=args.seed, reset=reset)
    except SeedError as e:
        print(e.message, file=sys.stderr)
        return 2
    if reset:
        with ctx.db.transaction() as conn:  # a rebuilt demo starts on real time, as the Reset button does
            conn.execute("DELETE FROM kv WHERE key = ?", (KV_OFFSET,))
    print(
        f"demo data written: seed {report.seed}, {report.counts['courses']} courses, digest {report.digest[:12]}"
    )
    print(f"staff logins (shown once; also in {logins_path(ctx)}, mode 0600):")
    for name, password in report.logins.items():
        print(f"  {name:<10} {password}")
    return 0


def _preflight(args: argparse.Namespace) -> int:
    from .preflight import run

    ok = asyncio.run(run(get_settings(), with_asr=args.with_asr))
    return 0 if ok else 1


def _create_admin(_: argparse.Namespace) -> int:
    from .api.admin_setup import SetupError, admin_exists, create_first_admin
    from .api.app import build_context

    ctx = build_context(get_settings())
    try:
        if admin_exists(ctx):  # asked first, so a refusal does not make the person type a password
            raise SetupError(
                "An administrator already exists. Sign in to the app as an administrator to add people."
            )
        username = input("Username for the first administrator: ")
        password = getpass.getpass("Password (not shown): ")
        if getpass.getpass("Password again: ") != password:
            raise SetupError("The two passwords are not the same.")
        create_first_admin(ctx, username, password)
    except SetupError as e:
        print(e, file=sys.stderr)
        return 2
    finally:
        ctx.db.close()
    print(f"Administrator {username.strip()} created. Sign in at the server's address.")
    return 0


def _purge(_: argparse.Namespace) -> int:
    from .api.app import build_context
    from .privacy import run_purges

    print(run_purges(build_context(get_settings())))
    return 0


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chaar-teeke")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the web app, the call runner and the scheduler")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    for name in ("seed-demo", "reset-demo"):
        sp = sub.add_parser(
            name,
            help="write the synthetic DEMO dataset"
            if name == "seed-demo"
            else "wipe and rewrite the DEMO dataset",
        )
        sp.add_argument("--seed", type=int, default=20261019)
    pf = sub.add_parser("preflight", help="check everything a demo needs and exit non-zero on a problem")
    pf.add_argument(
        "--with-asr",
        action="store_true",
        help="also load the speech model once and transcribe the first clip in data/demo/clips",
    )
    sub.add_parser("purge-now", help="run the deletion jobs once")
    sub.add_parser(
        "create-admin", help="make the first administrator (asks for a username and password; once only)"
    )
    a = p.parse_args(argv)
    if a.cmd == "serve":
        return _serve(a)
    if a.cmd == "seed-demo":
        return _seed(a, reset=False)
    if a.cmd == "reset-demo":
        return _seed(a, reset=True)
    if a.cmd == "preflight":
        return _preflight(a)
    if a.cmd == "create-admin":
        return _create_admin(a)
    return _purge(a)


if __name__ == "__main__":
    raise SystemExit(cli())
