from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console

from .planner import PlannerError
from .runner import run_task


def _run_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="askaround", description="Governance for agents that ask people.")
    ap.add_argument("task", help="what you want done")
    ap.add_argument("--roster", default="roster.yaml")
    ap.add_argument("--policy", default="policy.yaml")
    ap.add_argument("--receipt", default="receipt.json")
    ap.add_argument("--dry-run", action="store_true", help="fake Notify answers; no phones needed")
    ap.add_argument("--dry-run-silent", action="append", metavar="PERSON", help="person who ignores their first ask")
    ap.add_argument("--dry-run-deny", action="append", metavar="PERSON", help="person who denies")
    ap.add_argument("--speed", type=float, default=1.0, help="dry-run time multiplier (0.5 = twice as fast)")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    console = Console()
    if argv and argv[0] in ("policy", "verify", "mcp"):
        from . import extras
        return extras.dispatch(argv, console)
    a = _run_parser().parse_args(argv)
    try:
        receipt = asyncio.run(run_task(
            a.task, a.roster, a.policy, dry_run=a.dry_run, receipt_path=a.receipt, speed=a.speed,
            silent=set(a.dry_run_silent) if a.dry_run_silent else None,
            deny=set(a.dry_run_deny or []), console=console))
    except (PlannerError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    return 0 if receipt["outcome"] == "approved" else 1
