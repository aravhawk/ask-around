"""Live rich board."""
from __future__ import annotations

import asyncio

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STYLE = {"dropped": "dim strike", "waiting": "dim", "running": "yellow", "passed": "bold green", "failed": "bold red", "blocked": "bold magenta"}
VOTE = {"yes": "[green]✔[/]", "no": "[red]✘[/]", "blocked": "[magenta]silent[/]", "pending": "[yellow]…[/]",
        "cancelled": "[dim]–[/]", "not asked": "[dim]–[/]"}


class Board:
    def __init__(self, engine, console: Console | None = None):
        self.e, self.console = engine, console or Console()
        self.live: Live | None = None
        self._tick: asyncio.Task | None = None

    def render(self):
        e = self.e
        t = Table(title=Text(f"Ask Around · {e.task[:70]}"), expand=True)
        for c in ("Decision", "Owners", "Votes", "Status", "Left"):
            t.add_column(c)
        for d in e.states.values():
            owners = ", ".join(
                f"{escape(e.name(o))} {VOTE.get(d.votes.get(o, 'pending') if d.status != 'waiting' else 'not asked', '')}"
                for o in d.spec.owners)
            yes = sum(v == "yes" for k, v in d.votes.items() if k in d.spec.owners)
            left = d.seconds_left()
            dep = f" [dim](after {escape(', '.join(d.spec.depends_on))})[/]" if d.spec.depends_on else ""
            # every model/person/LLM-derived string is escaped: a stray "[/x]" must never crash the board
            t.add_row(f"[b]{escape(d.spec.id)}[/] {escape(d.spec.question[:60])}{dep}", owners,
                      f"{yes}/{d.spec.quorum}",
                      f"[{STYLE.get(d.status, '')}]{escape(d.status)}[/]"
                      + (f"\n[dim]{escape(d.reason[:40])}[/]" if d.reason else ""),
                      f"{left}s" if left is not None else "")
        parts = [t]
        if e.replans:
            parts.append(Text("re-plans: " + " | ".join(f"#{i+1} {r['summary']}" for i, r in enumerate(e.replans)),
                              style="cyan"))
        parts.append(Panel(Text("\n".join(e.log[-6:]) or "starting…"), title="log", border_style="dim"))
        return Group(*parts)

    async def _ticker(self):
        while True:
            await asyncio.sleep(1)
            self.refresh()

    def refresh(self):
        try:
            if self.live:
                self.live.update(self.render())
        except Exception:  # the board is cosmetic and must never break a run
            pass

    async def __aenter__(self):
        self.live = Live(self.render(), console=self.console, refresh_per_second=4)
        self.live.__enter__()
        self.e.on_change = self.refresh
        self._tick = asyncio.create_task(self._ticker())
        return self

    async def __aexit__(self, *a):
        self._tick.cancel()
        self.refresh()
        self.live.__exit__(*a)

    def summary(self, receipt: dict, path: str):
        c = self.console
        col = {"approved": "green", "rejected": "red", "blocked": "magenta"}[receipt["outcome"]]
        c.print(Panel(f"[bold {col}]{receipt['outcome'].upper()}[/]", title="Final outcome"))
        for d in receipt["decisions"]:
            c.print(f" [{STYLE.get(d['outcome'], '')}]{escape(d['outcome']):8}[/] {escape(d['id'])}: "
                    f"{escape(d['question'][:80])}" + (f"  [dim]({escape(d['reason'])})[/]" if d["reason"] else ""))
        c.print(f"\nReceipt written to [b]{escape(str(path))}[/] ({len(receipt['asks'])} asks)")
