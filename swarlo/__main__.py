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
    serve.add_argument("--git-dir", default="swarlo.git", help="Bare git repo path for DAG layer")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        from .sqlite_backend import SQLiteBackend
        from .git_dag import GitDAG
        from .server import app, set_backend, set_dag

        set_backend(SQLiteBackend(args.db))
        dag = GitDAG(args.git_dir)
        dag.init()
        set_dag(dag)
        print(f"Swarlo server starting on {args.host}:{args.port}")
        print(f"Database: {args.db}")
        print(f"Git repo: {args.git_dir}")
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
