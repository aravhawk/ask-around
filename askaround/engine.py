"""Quorum engine: parallel dispatch, veto, silence escalation, dependency gating."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import Plan, PlanDecision, Policy, Roster, validate_plan
from .notify import Notify, NotifyError, is_answer, is_consent

TERMINAL = {"passed", "failed", "blocked", "dropped"}


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
        self._replans_launched = 0

    # ---------- helpers ----------
    def note(self, msg: str) -> None:
        self.log.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        self._live_push()
        self._changed()

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:  # cosmetic callbacks (board) must never alter the consent flow
            pass

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
            ask_task = asyncio.ensure_future(self.notify.ask(
                p.webhook_url, title=f"Ask Around · {p.role}", body=prompt, type=type_, expires_s=expires_s,
                correlation_id=corr, idempotency_key=f"{self.run_id}-{corr}-{self._n}",
                device_id=p.device_id))
            try:
                rec.event_id, _ = await asyncio.shield(ask_task)
            except NotifyError as e:
                rec.status, rec.note = "error", str(e)
                self.note(f"{self.name(person_key)}: could not ask ({e})")
                return rec
            except asyncio.CancelledError:
                # cancelled while the POST was in flight: the prompt may already be on the person's phone.
                # Let the POST finish, record its eventId, and cancel it so no live prompt is left behind.
                rec.status, rec.note = "canceled", "canceled by Ask Around"
                try:
                    rec.event_id, _ = await asyncio.wait_for(asyncio.shield(ask_task), 25)
                    await self.notify.cancel(p.webhook_url, rec.event_id)
                except Exception:
                    pass
                raise
            rec.deadline = time.monotonic() + expires_s
            d.waiting_on[rec.correlation_id] = rec  # keyed per ask: a backup who is also an owner can't clobber
            if kind != "escalation":                 # escalation asks are not votes
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
                d.waiting_on.pop(rec.correlation_id, None)
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
                if is_answer(rec.status, spec.type, rec.text):
                    return "yes"
                if rec.status != "expired":
                    return "no"  # denied / no / canceled / empty reply / error / unknown: all NO
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
                if self.policy.veto_on_deny and any(r == "no" for _, r in results):  # veto takes precedence
                    d.status = "failed"
                    vetoer = next(o for o, r in results if r == "no")
                    d.reason = f"{self.name(vetoer)} said no (veto)"
                    return
                if not self.policy.veto_on_deny and any(r == "no" for _, r in results):
                    d.reason = d.reason or "denied (veto disabled by policy)"
                yes += sum(r == "yes" for _, r in results)
                if yes >= d.spec.quorum:
                    d.status = "passed"
                    return
                if yes + len(remaining) < d.spec.quorum:
                    if any(r == "no" for r in d.votes.values()):
                        d.status = "failed"  # denials made quorum unreachable
                    else:
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
                    if ds is None or ds.status in ("failed", "blocked", "dropped"):
                        d.status, d.reason, changed = "blocked", f"dependency {dep} did not pass", True
                        d.votes = {o: "not asked" for o in d.spec.owners}
                        self.note(f"{d.spec.id} blocked: dependency {dep}")
                        break

    def _ready(self) -> list[DState]:
        return [d for d in self.states.values() if d.status == "waiting"
                and all(self.states[x].status == "passed" for x in d.spec.depends_on if x in self.states)]

    async def run(self) -> dict:
        running: dict[asyncio.Task, DState] = {}
        replan_task: asyncio.Task | None = None
        replan_ctx: tuple[str, list[str]] | None = None
        settled_for_replan: DState | None = None
        try:
            await self._live_start()
            while True:
                self._gate()
                for d in self._ready():  # dispatch first: a slow re-plan never delays real asks
                    d.status = "running"
                    running[asyncio.create_task(self._run_decision(d))] = d
                await self._live_update()
                if settled_for_replan is not None and replan_task is None and self._should_replan():
                    self._replans_launched += 1
                    replan_ctx = (settled_for_replan.spec.id, sorted(self.states))
                    self.note(f"re-planning after {replan_ctx[0]} (runs in the background)…")
                    replan_task = asyncio.create_task(self.replanner.replan(
                        self.task, self._state_for_replanner(), self.roster, self.policy, set(self.states)))
                    settled_for_replan = None
                waitset = set(running) | ({replan_task} if replan_task else set())
                if not waitset:
                    break
                done, _ = await asyncio.wait(waitset, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is replan_task:
                        self._finish_replan(t, replan_ctx[0])
                        replan_task = replan_ctx = None
                        continue
                    d = running.pop(t)
                    exc = t.exception()
                    if exc:  # an engine bug must never become consent
                        d.status, d.reason = "failed", f"internal error: {type(exc).__name__}"
                    settled_for_replan = d
        finally:
            for t in list(running) + ([replan_task] if replan_task else []):
                t.cancel()
            pending = list(running) + ([replan_task] if replan_task else [])
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for d in self.states.values():
            if d.status in ("waiting", "running"):
                d.status, d.reason = "blocked", d.reason or "never ran"
        await self._live_end()
        self._changed()
        return self.receipt()

    # ---------- re-planner ----------
    def _state_for_replanner(self) -> list[dict]:
        out = []
        for d in self.states.values():
            status = {"waiting": "pending", "running": "in_flight"}.get(d.status, d.status)
            answers = [{"person": a.person, "status": a.status, "text": (a.text or "")[:300]}
                       for a in d.asks if a.status not in ("canceled",)]
            out.append({"id": d.spec.id, "question": d.spec.question, "type": d.spec.type, "owners": d.spec.owners,
                        "quorum": d.spec.quorum, "depends_on": d.spec.depends_on, "status": status,
                        "answers": answers if status in ("passed", "failed", "blocked") else []})
        return out

    def _should_replan(self) -> bool:
        """Only while the run can still be approved: a failed/blocked decision means no re-plan can help,
        and skipping it also removes that attack surface."""
        if not self.replanner or self._replans_launched >= self.policy.max_replans:
            return False
        sts = [d.status for d in self.states.values()]
        return "waiting" in sts and not any(x in ("failed", "blocked") for x in sts)

    def _finish_replan(self, task: asyncio.Task, after: str) -> None:
        entry = {"after": after, "applied": False, "added": [], "edited": [], "dropped": [], "skipped": [],
                 "proposed": {}, "reason": "", "summary": ""}
        try:
            rp = task.result()
            entry["proposed"] = {"added": [a.id for a in rp.add], "edited": [e.get("id") for e in rp.edit],
                                 "dropped": list(rp.drop)}
            self._apply_replan(rp, entry)
        except Exception as e:  # a failed or rejected re-plan keeps the existing plan; it never approves anything
            msg = str(e)[:160] if isinstance(e, ValueError) else type(e).__name__
            entry["summary"] = f"re-plan rejected ({msg}); plan unchanged"
            entry["applied"] = False
            entry["added"], entry["edited"], entry["dropped"] = [], [], []  # nothing was applied
        self.replans.append(entry)
        self.note(f"re-plan {len(self.replans)}: {entry['summary']}")

    EDITABLE = ("question", "type", "owners", "quorum", "depends_on", "expires_s")

    def _check_not_weaker(self, old: PlanDecision, new: PlanDecision) -> None:
        """A re-plan may only tighten a pending decision, never loosen it."""
        new_owners = set()
        for o in new.owners:
            k = self.roster.resolve(o)
            if k is None:
                raise ValueError(f"{old.id}: unknown owner '{o}'")
            new_owners.add(k)
        if not set(old.owners) <= new_owners:
            raise ValueError(f"{old.id}: re-plan may not remove owners")
        if new.quorum < old.quorum:
            raise ValueError(f"{old.id}: re-plan may not lower quorum")
        if old.type != "text" and new.type == "text":
            raise ValueError(f"{old.id}: re-plan may not turn a consent question into free text")
        if not set(old.depends_on) <= set(new.depends_on):
            raise ValueError(f"{old.id}: re-plan may not remove dependencies")

    def _apply_replan(self, rp, entry: dict) -> None:
        waiting = {i for i, d in self.states.items() if d.status == "waiting"}  # ONLY these may change, checked NOW
        specs = {i: self.states[i].spec.model_copy(deep=True) for i in waiting}
        fixed = {i for i in self.states if i not in waiting}
        added, edited, dropped, skipped = [], [], [], []
        for e in rp.edit:
            i = e.get("id")
            if i not in waiting:  # settled or already started while the re-plan was running: stale, skip it
                skipped.append(str(i))
                continue
            old = self.states[i].spec
            new = PlanDecision.model_validate({**old.model_dump(), **{k: e[k] for k in self.EDITABLE if k in e}})
            self._check_not_weaker(old, new)
            specs[i] = new
            edited.append(i)
        for i in rp.drop:
            if i not in waiting:
                skipped.append(str(i))
                continue
            if self.states[i].spec.type != "text":
                raise ValueError(f"{i}: only informational (text) decisions may be dropped")
            specs.pop(i, None)
            dropped.append(i)
        for a in rp.add:
            if a.id in self.states or a.id in specs:
                raise ValueError(f"re-plan reused id {a.id}")
            specs[a.id] = a
            added.append(a.id)
        # validate the WHOLE resulting graph (owners, quorum, deps, cycles, policy rules) before applying
        candidate = [s.model_copy(deep=True) for s in specs.values()]
        validate_plan(Plan(decisions=candidate), self.roster, self.policy, existing_ids=fixed)
        for c in candidate:
            if c.id in self.states:
                self.states[c.id].spec = c
            else:
                self.states[c.id] = DState(c)
        for i in dropped:
            self.states[i].status, self.states[i].reason = "dropped", "dropped by re-plan"
        # only now (success) does the audit record claim changes
        entry.update(applied=True, added=added, edited=edited, dropped=dropped, skipped=skipped)
        bits = [f"+{len(added)}" if added else "", f"~{len(edited)}" if edited else "", f"-{len(dropped)}" if dropped else ""]
        entry["reason"] = (rp.reason or "")[:200]
        entry["summary"] = (" ".join(b for b in bits if b) or "no change") \
            + (f" ({len(skipped)} stale skipped)" if skipped else "") + (f": {entry['reason']}" if entry["reason"] else "")

    # ---------- Live Activity (best effort; never raises) ----------
    def _live_text(self) -> tuple[str, float]:
        live = [d for d in self.states.values() if d.status != "dropped"]
        decided = sum(d.status in ("passed", "failed", "blocked") for d in live)
        waiting = sorted({self.name(a.person) for d in live for a in d.waiting_on.values()})
        text = f"{decided} of {len(live)} decided"
        if waiting:
            text += " · waiting on " + ", ".join(waiting)
        return text, (decided / len(live) if live else 1.0)

    def _final_text(self) -> str:
        out = self.outcome()
        if out == "approved":
            who = []
            for d in self.states.values():
                for o, v in d.votes.items():
                    if v == "yes" and self.name(o) not in who:
                        who.append(self.name(o))
            return "Approved" + (f" by {' + '.join(who)}" if who else "") + " · receipt saved"
        reasons = [d.reason for d in self.states.values() if d.reason and d.status in ("failed", "blocked")]
        return f"{out.capitalize()}" + (f" · {reasons[0]}" if reasons else "") + " · receipt saved"

    def _live_push(self) -> None:
        try:
            if self.live:
                text, prog = self._live_text()
                self.live.push(text, prog)
        except Exception:
            pass

    async def _live_start(self) -> None:
        try:
            if self.live:
                text, prog = self._live_text()
                await self.live.start(text, prog)
        except Exception:
            pass

    async def _live_update(self) -> None:
        self._live_push()

    async def _live_end(self) -> None:
        try:
            if self.live:
                await self.live.end(self._final_text())
        except Exception:
            pass

    def outcome(self) -> str:
        sts = [d.status for d in self.states.values() if d.status != "dropped"]
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
