"""Quorum engine: parallel dispatch, veto, silence escalation, dependency gating."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import Plan, PlanDecision, Policy, Roster
from .notify import Notify, NotifyError, is_consent

TERMINAL = {"passed", "failed", "blocked"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AskRecord:
    decision: str
    person: str
    kind: str  # primary | reask | escalation
    prompt: str
    correlation_id: str
    event_id: str | None = None
    status: str = "pending"
    responded_at: str | None = None
    text: str | None = None
    sent_at: str = field(default_factory=now_iso)
    deadline: float | None = None  # monotonic, local only
    note: str | None = None

    def public(self) -> dict:
        return {"decision": self.decision, "person": self.person, "kind": self.kind, "prompt": self.prompt,
                "eventId": self.event_id, "status": self.status, "respondedAt": self.responded_at,
                "text": self.text, "sentAt": self.sent_at, "note": self.note}


@dataclass
class DState:
    spec: PlanDecision
    status: str = "waiting"  # waiting | running | passed | failed | blocked
    votes: dict[str, str] = field(default_factory=dict)  # owner -> yes|no|blocked|cancelled|pending
    reason: str = ""
    asks: list[AskRecord] = field(default_factory=list)
    waiting_on: dict[str, AskRecord] = field(default_factory=dict)

    def seconds_left(self) -> int | None:
        ds = [a.deadline for a in self.waiting_on.values() if a.deadline]
        return max(0, int(min(ds) - time.monotonic())) if ds else None


class Engine:
    def __init__(self, task: str, plan: Plan, roster: Roster, policy: Policy, notify: Notify, *,
                 poll_interval: float = 2.0, on_change: Callable[[], None] | None = None,
                 replanner: Any = None, live: Any = None):
        self.task, self.roster, self.policy, self.notify = task, roster, policy, notify
        self.poll_interval = poll_interval
        self.on_change = on_change or (lambda: None)
        self.replanner, self.live = replanner, live
        self.run_id = uuid.uuid4().hex[:10]
        self.initial_plan = plan.model_dump()
        self.states: dict[str, DState] = {d.id: DState(d) for d in plan.decisions}
        self.replans: list[dict] = []
        self.log: list[str] = []
        self.started_at = now_iso()
        self._n = 0

    # ---------- helpers ----------
    def note(self, msg: str) -> None:
        self.log.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        self.on_change()

    def name(self, key: str) -> str:
        return self.roster.people[key].name

    # ---------- one ask ----------
    async def _ask(self, d: DState, person_key: str, prompt: str, type_: str, expires_s: int, kind: str,
                   allowed_user_ids: set[str] | None = None) -> AskRecord:
        """Send one ask and wait for it to settle. The returned status is normalised: only a
        server-confirmed approved/yes is ever consent; everything else is NO-equivalent."""
        p = self.roster.people[person_key]
        misroutes = 0
        while True:
            self._n += 1
            corr = f"{d.spec.id}-{person_key}" + ("" if kind == "primary" else f"-{kind}{self._n}")
            rec = AskRecord(d.spec.id, person_key, kind, prompt, corr)
            d.asks.append(rec)
            try:
                rec.event_id, _ = await self.notify.ask(
                    p.webhook_url, title=f"Ask Around · {p.role}", body=prompt, type=type_, expires_s=expires_s,
                    correlation_id=corr, idempotency_key=f"{self.run_id}-{corr}-{self._n}")
            except NotifyError as e:
                rec.status, rec.note = "error", str(e)
                self.note(f"{self.name(person_key)}: could not ask ({e})")
                return rec
            rec.deadline = time.monotonic() + expires_s
            d.waiting_on[person_key] = rec
            d.votes.setdefault(person_key, "pending")
            self.note(f"asked {self.name(person_key)} [{d.spec.id}]")
            try:
                st = await self._poll(p.webhook_url, rec, expires_s)
            except asyncio.CancelledError:
                rec.status = "canceled"
                rec.note = "canceled by Ask Around"
                await self.notify.cancel(p.webhook_url, rec.event_id)
                raise
            finally:
                d.waiting_on.pop(person_key, None)
            rec.status, rec.responded_at, rec.text = st.status, st.responded_at, st.text
            # wrong-person guard: only honour answers from an expected user when the server tells us who answered
            if (st.responding_user_id and allowed_user_ids is not None
                    and st.responding_user_id not in allowed_user_ids):
                rec.note = "ignored: answered by someone who is not allowed to answer"
                rec.status = "ignored_wrong_responder"
                misroutes += 1
                self.note(f"ignored an answer from the wrong person on [{d.spec.id}]; re-asking")
                if misroutes > 2:
                    rec.status = "error"
                    return rec
                continue
            return rec

    async def _poll(self, webhook: str, rec: AskRecord, expires_s: int):
        from .notify import EventState

        hard_stop = time.monotonic() + expires_s + 45
        errors = 0
        while True:
            try:
                st = await self.notify.read(webhook, rec.event_id)
                errors = 0
            except NotifyError:
                errors += 1
                if errors >= 4:
                    return EventState(status="error")
                st = None
            if st is not None and st.status != "pending":
                return st
            if time.monotonic() > hard_stop:
                return EventState(status="expired")
            await asyncio.sleep(self.poll_interval)

    # ---------- one owner ----------
    async def _owner_flow(self, d: DState, owner: str) -> str:
        spec, person = d.spec, self.roster.people[owner]
        reasks = 0
        try:
            while True:
                rec = await self._ask(d, owner, spec.question, spec.type, spec.expires_s,
                                      "primary" if reasks == 0 else "reask",
                                      {person.user_id} if person.user_id else None)
                if is_consent(rec.status):
                    return "yes"
                if rec.status != "expired":
                    return "no"  # denied / no / canceled / replied / error / unknown: all NO
                # silence -> escalate to backup
                if not person.backup or reasks >= self.policy.max_reasks:
                    d.reason = d.reason or f"{person.name} did not answer"
                    return "blocked"
                bk = self.roster.people[person.backup]
                brec = await self._ask(
                    d, person.backup, f"{person.name} hasn't answered: {spec.question}. Ask again?", "yes_no",
                    spec.expires_s, "escalation", {bk.user_id} if bk.user_id else None)
                if not is_consent(brec.status):
                    d.reason = d.reason or f"{person.name} silent and {bk.name} did not approve a re-ask"
                    return "blocked"
                reasks += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a bug become an approval
            self.note(f"internal error on {spec.id}/{owner}: {type(e).__name__} -> NO")
            return "no"

    # ---------- one decision ----------
    async def _run_decision(self, d: DState) -> None:
        d.status = "running"
        self.note(f"{d.spec.id} started")
        tasks = {asyncio.create_task(self._owner_flow(d, o)): o for o in d.spec.owners}
        remaining, yes = set(tasks), 0
        try:
            while remaining:
                done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
                results = [(tasks[t], t.result()) for t in done]
                for o, r in results:
                    d.votes[o] = r
                if any(r == "no" for _, r in results):  # veto takes precedence
                    d.status = "failed"
                    vetoer = next(o for o, r in results if r == "no")
                    d.reason = f"{self.name(vetoer)} said no (veto)"
                    return
                yes += sum(r == "yes" for _, r in results)
                if yes >= d.spec.quorum:
                    d.status = "passed"
                    return
                if yes + len(remaining) < d.spec.quorum:
                    d.status = "blocked"
                    d.reason = d.reason or "quorum can no longer be reached"
                    return
            d.status = "blocked"
        finally:
            for t in remaining:
                t.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
                for t in remaining:
                    d.votes[tasks[t]] = "cancelled"
            if d.status == "running":
                d.status = "blocked"
            self.note(f"{d.spec.id} {d.status}" + (f": {d.reason}" if d.reason else ""))

    # ---------- whole run ----------
    def _gate(self) -> None:
        """Block waiting decisions whose dependencies failed or were blocked."""
        changed = True
        while changed:
            changed = False
            for d in self.states.values():
                if d.status != "waiting":
                    continue
                for dep in d.spec.depends_on:
                    ds = self.states.get(dep)
                    if ds is None or ds.status in ("failed", "blocked"):
                        d.status, d.reason, changed = "blocked", f"dependency {dep} did not pass", True
                        d.votes = {o: "not asked" for o in d.spec.owners}
                        self.note(f"{d.spec.id} blocked: dependency {dep}")
                        break

    def _ready(self) -> list[DState]:
        return [d for d in self.states.values() if d.status == "waiting"
                and all(self.states[x].status == "passed" for x in d.spec.depends_on if x in self.states)]

    async def run(self) -> dict:
        running: dict[asyncio.Task, DState] = {}
        try:
            await self._live_start()
            while True:
                self._gate()
                for d in self._ready():
                    d.status = "running"
                    running[asyncio.create_task(self._run_decision(d))] = d
                await self._live_update()
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    d = running.pop(t)
                    exc = t.exception()
                    if exc:  # an engine bug must never become consent
                        d.status, d.reason = "failed", f"internal error: {type(exc).__name__}"
                    await self._maybe_replan(d)
        finally:
            for t in running:
                t.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        for d in self.states.values():
            if d.status in ("waiting", "running"):
                d.status, d.reason = "blocked", d.reason or "never ran"
        await self._live_end()
        self.on_change()
        return self.receipt()

    # hooks filled in by tier 2
    async def _maybe_replan(self, settled: DState) -> None:
        return None

    async def _live_start(self) -> None:
        return None

    async def _live_update(self) -> None:
        return None

    async def _live_end(self) -> None:
        return None

    def outcome(self) -> str:
        sts = [d.status for d in self.states.values()]
        if sts and all(s == "passed" for s in sts):
            return "approved"
        return "rejected" if any(s == "failed" for s in sts) else "blocked"

    def receipt(self) -> dict:
        return {
            "task": self.task,
            "runId": self.run_id,
            "startedAt": self.started_at,
            "finishedAt": now_iso(),
            "plan": self.initial_plan,
            "replans": self.replans,
            "decisions": [
                {"id": d.spec.id, "question": d.spec.question, "owners": d.spec.owners, "quorum": d.spec.quorum,
                 "dependsOn": d.spec.depends_on, "outcome": d.status, "votes": d.votes, "reason": d.reason}
                for d in self.states.values()
            ],
            "asks": [a.public() for d in self.states.values() for a in d.asks],
            "outcome": self.outcome(),
        }
