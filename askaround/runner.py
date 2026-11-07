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
from .signing import sign_receipt
from .planner import Kai, PlannerError, canned_plan


async def run_task(task: str, roster_path: str, policy_path: str | None, *, dry_run: bool = False,
                   silent: set[str] | None = None, deny: set[str] | None = None, receipt_path: str | None = "receipt.json",
                   show_board: bool = True, console: Console | None = None, poll_interval: float = 2.0,
                   speed: float = 1.0) -> dict:
    console = console or Console()
    roster = load_roster(roster_path)
    if policy_path is None and Path("policy.yaml").is_file():
        policy_path = "policy.yaml"
    policy = load_policy(policy_path).bind(roster)  # explicit-but-missing path raises; typo'd owners raise
    if policy_path is None:
        console.print("[yellow]warning:[/] no policy file: running with built-in defaults and NO policy rules")
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
        replanner = kai if kai.api_key else None  # no key (offline dry-run): plan stays as is
        engine = Engine(task, plan, roster, policy, Notify(http), poll_interval=poll_interval, replanner=replanner)
        if roster.requester:
            from .liveactivity import LiveActivity
            engine.live = LiveActivity(http, roster.people[roster.requester].webhook_url, engine.run_id,
                                       title=task[:60])
        if show_board:
            async with Board(engine, console) as board:
                receipt = await engine.run()
        else:
            receipt = await engine.run()
    finally:
        await http.aclose()
        await kai.client.aclose()
    receipt = sign_receipt(receipt)  # no-op unless ASKAROUND_SIGNING_KEY is set
    receipt_path = receipt_path or f"receipt-{receipt['runId']}.json"
    Path(receipt_path).write_text(json.dumps(receipt, indent=2))
    if show_board:
        board.summary(receipt, receipt_path)
    return receipt
