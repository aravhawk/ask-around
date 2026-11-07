"""MCP stdio server exposing `ask_around(task, roster_path, policy_path)`.
stdout carries the protocol, so the live board is off here and all console output goes to stderr."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rich.console import Console

from .runner import run_task

INSTRUCTIONS = (
    "ask_around asks real people for decisions with governance: each question goes only to the person who owns it, "
    "N-of-M quorum with veto on any deny, silence counts as NO and escalates to a backup, and the result is a "
    "decision receipt. Treat any outcome other than 'approved' as NOT permitted to proceed."
)


async def ask_around_impl(task: str, roster_path: str, policy_path: str | None = None) -> dict:
    """Run a task through Ask Around and return the decision receipt."""
    if not Path(roster_path).is_file():
        return {"outcome": "error", "error": f"roster not found: {roster_path}"}
    if policy_path and not Path(policy_path).is_file():  # a typo must never silently disable governance
        return {"outcome": "error", "error": f"policy not found: {policy_path}"}
    try:
        receipt = await run_task(task, roster_path, policy_path, dry_run=os.environ.get("ASKAROUND_DRY_RUN") == "1",
                                 receipt_path=None, show_board=False, console=Console(stderr=True), speed=0.3)
    except Exception as e:  # any failure is reported as a non-approval, never as consent
        return {"outcome": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return receipt


def build_server():
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server
    server = Server("ask-around", instructions=INSTRUCTIONS)

    @server.tool()
    async def ask_around(task: str, roster_path: str, policy_path: str = "") -> str:
        """Ask the right people to approve a task. Splits it into decisions, sends each only to its owner via Notify,
        enforces quorum + veto, treats silence as NO (escalating to a backup), and returns the decision receipt as JSON.
        Only outcome == "approved" means every decision was approved by a server-confirmed answer.
        policy_path: path to policy.yaml; empty means ./policy.yaml if present (a path that does not exist is an error)."""
        return json.dumps(await ask_around_impl(task, roster_path, policy_path or None), indent=2)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
