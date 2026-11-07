"""Subcommands: `askaround policy`, `askaround verify`, `askaround mcp`."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

import yaml
from rich.console import Console

from .models import load_roster
from .planner import Kai, PlannerError


async def compile_policy_cmd(text: str, roster_path: str | None, out: str, console: Console) -> int:
    roster = load_roster(roster_path) if roster_path and Path(roster_path).exists() else None
    kai = Kai()
    try:
        policy = await kai.compile_policy(text, roster)
    except PlannerError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    finally:
        await kai.client.aclose()
    body = "# Compiled from plain English by Belweave Kai-4:\n# " + text.replace("\n", " ")[:200] + "\n" + \
        yaml.safe_dump(policy.as_yaml_dict(), sort_keys=False)
    if out == "-":
        console.print(body, markup=False, highlight=False)
        return 0
    path = Path(out)
    if path.exists():
        shutil.copy(path, f"{out}.bak")
        console.print(f"[dim]existing {out} saved as {out}.bak[/]")
    path.write_text(body)
    console.print(f"wrote [b]{out}[/] with {len(policy.rules)} rule(s)")
    return 0


def dispatch(argv: list[str], console: Console) -> int:
    cmd, rest = argv[0], argv[1:]
    if cmd == "policy":
        ap = argparse.ArgumentParser(prog="askaround policy", description="Compile plain English into policy.yaml")
        ap.add_argument("text")
        ap.add_argument("--roster", default="roster.yaml")
        ap.add_argument("-o", "--out", default="policy.yaml", help="output path, or - for stdout")
        a = ap.parse_args(rest)
        return asyncio.run(compile_policy_cmd(a.text, a.roster, a.out, console))
    if cmd == "verify":
        from .signing import verify_cmd
        return verify_cmd(rest, console)
    if cmd == "mcp":
        from .mcp_server import main
        return main()
    return 2
