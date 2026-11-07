"""Glue shared by the CLI and the MCP server."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from rich.console import Console

from .board import Board
from .engine import Engine
from .fake import FakeNotify, demo_script
from .models import load_policy, load_roster
from .notify import Notify
from .planner import Kai, PlannerError, canned_plan


async def run_task(task: str, roster_path: str, policy_path: str | None, *, dry_run: bool = False,
                   silent: set[str] | None = None, deny: set[str] | None = None, receipt_path: str = "receipt.json",
                   show_board: bool = True, console: Console | None = None, poll_interval: float = 2.0,
                   speed: float = 1.0) -> dict:
    roster, policy = load_roster(roster_path), load_policy(policy_path)
    console = console or Console()
    kai = Kai()
    if dry_run:
        for k, p in roster.people.items():
            p.webhook_url = f"https://fake.notify/{k}"
        keys = [k for k in roster.people if k != roster.requester]
        silent = silent if silent is not None else set(keys[1:2])
        fake = FakeNotify(demo_script(silent, deny or set(), speed))
        http = httpx.AsyncClient(transport=fake.transport())
        poll_interval = min(poll_interval, 0.5)
    else:
        http = httpx.AsyncClient()
    try:
        if dry_run and not kai.api_key:
            console.print("[dim]dry-run without BELWEAVE_API_KEY: using the built-in offline plan[/]")
            plan = canned_plan(task, roster, policy)
        else:
            console.print("[dim]planning with Belweave Kai-4…[/]")
            plan = await kai.plan(task, roster, policy)
        engine = Engine(task, plan, roster, policy, Notify(http), poll_interval=poll_interval)
        _install_tier2(engine, kai, http, dry_run)
        if show_board:
            async with Board(engine, console) as board:
                receipt = await engine.run()
            board.summary(receipt, receipt_path)
        else:
            receipt = await engine.run()
    finally:
        await http.aclose()
        await kai.client.aclose()
    Path(receipt_path).write_text(json.dumps(receipt, indent=2))
    return receipt


def _install_tier2(engine: Engine, kai: Kai, http, dry_run: bool) -> None:  # filled in by tier 2
    return None
