"""Local CLI: explicit storage setup, one-shot requests, or a single-customer chat."""

import argparse
import sys
from uuid import uuid4

from .config import Config
from .storage import initialize, seed_demo
from .tools import Session


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AWS Strands customer-support test agent")
    parser.add_argument("--config", default="config.toml", help="TOML configuration path")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db", help="Create empty local databases without calling AWS")
    init.add_argument("--demo", action="store_true", help="Add fictional customer/order records")
    for command in ("ask", "chat"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--customer-id", required=True, help="Trusted local operator's customer selection")
        subparser.add_argument("--interaction-id", default=None)
        if command == "ask":
            subparser.add_argument("prompt")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.command == "init-db":
            if config.dynamodb_table:
                raise ValueError("init-db initializes SQLite only; use Terraform to create and seed DynamoDB")
            initialize(config.database_path, config.interaction_record_path)
            if args.demo:
                seed_demo(config.database_path)
            print("Local storage initialized" + (" with fictional demo records." if args.demo else "."))
            return 0
        session = Session(args.customer_id, args.interaction_id or str(uuid4()))
        from .agent import build_agent, invoke

        print(f"Customer: {session.customer_id} | Interaction: {session.interaction_id}", file=sys.stderr)
        if args.command == "ask":
            print(invoke(args.prompt, config, session))
        else:
            agent = build_agent(config, session)
            print("Enter a message. /close asks the agent to save a summary and exits; /quit exits without saving.")
            while True:
                try:
                    prompt = input("You: ").strip()
                except EOFError:
                    break
                if prompt == "/quit":
                    break
                if not prompt:
                    continue
                closing = prompt == "/close"
                if closing:
                    prompt = "I am explicitly closing this interaction. Please save an interaction summary."
                print(f"Agent: {agent(prompt)}")
                if closing:
                    break
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Inspect the ledger and interaction records before retrying a write.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print raw AWS exceptions that may contain request data or credentials.
        if isinstance(exc, (ValueError, TypeError, FileNotFoundError)):
            print(f"Configuration/input error: {exc}", file=sys.stderr)
        else:
            print(f"Agent failed ({type(exc).__name__}). Check AWS access and local storage. "
                  "Completed tools remain recorded; inspect records before retrying writes.", file=sys.stderr)
        return 1
