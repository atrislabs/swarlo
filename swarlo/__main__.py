"""CLI entry point: python -m swarlo serve --port 8080"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Swarlo — agent coordination protocol")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the Swarlo server")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--db", default="swarlo.db", help="SQLite database path")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        from .sqlite_backend import SQLiteBackend
        from .server import app, set_backend

        set_backend(SQLiteBackend(args.db))
        print(f"Swarlo server starting on {args.host}:{args.port}")
        print(f"Database: {args.db}")
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
