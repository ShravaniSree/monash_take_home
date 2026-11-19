"""Command line entry for the ADK accounts-payable workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ap_agent.application.service import build_service
from ap_agent.utils.env import load_local_env
from ap_agent.domain.models import ApprovalDecision
from ap_agent.systems.fixtures import request_from_payload
from ap_agent.systems.fixtures import scenario_path


def decision_from_cli(args: argparse.Namespace) -> ApprovalDecision:
    """Read an approval from a JSON file or from individual flags.

    Args:
        args: Parsed ``decide`` arguments.

    Returns:
        The validated decision.

    Raises:
        SystemExit: Neither a file nor the required flags were provided.
    """
    if args.decision is not None:
        return ApprovalDecision.model_validate_json(args.decision.read_text(encoding="utf-8"))
    if args.action and args.approver_id and args.role:
        return ApprovalDecision(
            action=args.action,
            approver_id=args.approver_id,
            role=args.role,
            comment=args.comment,
        )
    raise SystemExit("Pass a decision JSON file, or --action, --approver-id, and --role")


def main() -> None:
    """Dispatch ``start``, ``decide``, ``get``, and ``serve``."""
    parser = argparse.ArgumentParser(description="Northstar AP agent (Google ADK)")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Run a case until it completes or waits")
    start.add_argument("request", nargs="?", type=Path, help="Case request JSON. Omit when using --scenario.")
    start.add_argument("--scenario", help="Load request and tool mocks from fixtures/scenarios/{id}.json")

    decide = commands.add_parser("decide", help="Approve or reject a waiting run")
    decide.add_argument("run_id")
    decide.add_argument(
        "decision",
        nargs="?",
        type=Path,
        help="JSON file with the same body as POST /runs/{run_id}/decision",
    )
    decide.add_argument("--action", choices=["APPROVED", "REJECTED"])
    decide.add_argument("--approver-id")
    decide.add_argument("--role")
    decide.add_argument("--comment", default="")

    show = commands.add_parser("get", help="Show the stored run result")
    show.add_argument("run_id")

    serve = commands.add_parser("serve", help="Start the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    load_local_env(args.root)
    if args.command == "serve":
        import uvicorn

        from ap_agent.api import create_app

        uvicorn.run(create_app(args.root), host=args.host, port=args.port)
        return

    service = build_service(args.root)

    async def _go() -> None:
        """Run the selected command and print the run snapshot."""
        if args.command == "start":
            if args.scenario:
                payload = json.loads(scenario_path(args.scenario).read_text(encoding="utf-8"))
            elif args.request is not None:
                payload = json.loads(args.request.read_text(encoding="utf-8"))
            else:
                raise SystemExit("Pass a request JSON file or --scenario FIN-001")
            request = request_from_payload(payload)
            result = await service.start(request)
        elif args.command == "decide":
            result = await service.decide(args.run_id, decision_from_cli(args))
        else:
            result = await service.get(args.run_id)
        print(json.dumps(result.model_dump(mode="json"), indent=2))

    asyncio.run(_go())
