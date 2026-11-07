"""Kai-4 planner and re-planner. The model only ever sees roster keys/roles, never webhooks."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from .models import Plan, PlanDecision, Policy, Roster, validate_plan

URL = "https://api.infer.belweave.ai/v1/chat/completions"
MODEL = "Belweave/Kai-4"
_SEM = asyncio.Semaphore(2)  # 2 concurrent streams

PLAN_SYSTEM = """You are the planner inside Ask Around, a system that asks real people for decisions.
Split the user's task into the smallest set of DECISIONS a human must make. Each decision is sent only to the people who own it.
Return JSON ONLY (no prose, no code fences) shaped exactly:
{"decisions":[{"id":"d1","question":"...","type":"approval"|"yes_no"|"text","owners":["<roster key>"],"quorum":1,"depends_on":[],"expires_s":30}]}
Rules:
- owners must be roster keys. quorum is how many owners must say yes (1..len(owners)). Any single no vetoes the decision.
- question must be a short, self-contained, answerable yes/no or approve/deny question that includes concrete numbers from the task when available. Never invent facts.
- depends_on lists ids of decisions that must be approved first (e.g. book only after budget is approved).
- Use type "approval" for anything that needs consent (money, access, bookings), "yes_no" for simple questions, and "text" ONLY to gather information (e.g. a preference). A text reply is information, never consent.
- Ask the fewest people possible, never anyone outside the roster, and apply every policy rule (rule.match / owners / quorum). Put irreversible decisions after cheap ones.
- question must be at most 200 characters.
- 2 to 6 decisions. expires_s between 30 and 86400.
"""

REPLAN_SYSTEM = """You are the re-planner inside Ask Around. Some decisions have settled. You may adjust ONLY decisions with status "pending".
Return JSON ONLY: {"add":[<decision objects>],"edit":[{"id":"...", "question":"...", "owners":[...], "quorum":N, "depends_on":[...]}],"drop":["id",...],"reason":"short"}
Decision object shape: {"id","question","type","owners","quorum","depends_on","expires_s"}. New ids must be unique. Never reference settled decisions as editable. If no change is needed return {"add":[],"edit":[],"drop":[],"reason":"no change"}.
"""


class Replan(BaseModel):
    add: list[PlanDecision] = []
    edit: list[dict[str, Any]] = []
    drop: list[str] = []
    reason: str = ""


class PlannerError(Exception):
    pass


def _extract_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i : j + 1])
        raise


class Kai:
    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("BELWEAVE_API_KEY", "")
        self.client = client or httpx.AsyncClient()

    async def _chat(self, system: str, user: str) -> str:
        if not self.api_key:
            raise PlannerError("BELWEAVE_API_KEY is not set")
        body = {"model": MODEL, "max_tokens": 4000, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with _SEM:
            for attempt in range(3):
                try:
                    r = await self.client.post(URL, json=body, timeout=180,
                                               headers={"Authorization": f"Bearer {self.api_key}"})
                except httpx.HTTPError as e:  # timeouts / connect errors: a planner failure, not a crash
                    raise PlannerError(f"Kai-4 request failed: {type(e).__name__}") from e
                if r.status_code == 429 and attempt < 2:
                    try:
                        wait = float(r.json().get("retryAfterSeconds", 20))
                    except Exception:
                        wait = 20
                    await asyncio.sleep(min(wait, 60))
                    continue
                break
        if r.status_code >= 300:
            raise PlannerError(f"Kai-4 http {r.status_code}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            raise PlannerError("Kai-4 returned an unexpected response shape") from e

    async def _json_call(self, system: str, user: str, parse):
        last = ""
        for attempt in range(2):  # one retry on parse failure
            msg = user if attempt == 0 else f"{user}\n\nYour previous reply was invalid ({last}). Return valid JSON only."
            text = await self._chat(system, msg)
            try:
                return parse(_extract_json(text))
            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                last = str(e)[:300].replace("\n", " ")
        raise PlannerError(f"Kai-4 output failed validation twice: {last}")

    async def plan(self, task: str, roster: Roster, policy: Policy) -> Plan:
        user = json.dumps({"task": task, "roster": roster.public_summary(), "policy_rules": policy.rules_for_planner(),
                           "default_expires_s": policy.default_expires_s})

        def parse(data):
            return validate_plan(Plan.model_validate(data), roster, policy)

        return await self._json_call(PLAN_SYSTEM, user, parse)

    async def replan(self, task: str, state: list[dict], roster: Roster, policy: Policy,
                     existing_ids: set[str]) -> Replan:
        user = json.dumps({"task": task, "roster": roster.public_summary(), "policy_rules": policy.rules_for_planner(),
                           "state": state})
        pending = {s["id"] for s in state if s["status"] == "pending"}

        def parse(data):
            rp = Replan.model_validate(data)
            for e in rp.edit:
                if e.get("id") not in pending:
                    raise ValueError(f"cannot edit non-pending decision {e.get('id')}")
            rp.drop = [d for d in rp.drop if d in pending]
            validate_plan(Plan(decisions=rp.add), roster, policy, existing_ids - set())
            return rp

        return await self._json_call(REPLAN_SYSTEM, user, parse)

    async def compile_policy(self, text: str, roster: Roster | None = None) -> Policy:
        """Plain English -> Policy (PRD section 7 shape). Owners are checked against the roster when given."""
        system = (
            "Compile the plain-English approval policy into JSON ONLY (no prose, no code fences) shaped exactly: "
            '{"rules":[{"match":"<short condition, e.g. money > 200 or hotel>","owners":["<roster key>"],"quorum":<int>}],'
            '"defaults":{"expires_s":<30-86400>,"veto_on_deny":true,"max_reasks":<0-5>,"max_replans":<0-2>}}. '
            'Example: "budget over $200 needs both parents" with roster keys mom, dad -> '
            '{"rules":[{"match":"money > 200","owners":["mom","dad"],"quorum":2}],'
            '"defaults":{"expires_s":300,"veto_on_deny":true,"max_reasks":1}}. '
            "quorum must be between 1 and len(owners). Use ONLY roster keys as owners. Omit anything the text does not say; "
            "never loosen safety: veto_on_deny stays true unless the text explicitly says a single no should not block."
        )
        user = json.dumps({"policy_text": text, "roster": roster.public_summary() if roster else None})

        def parse(data):
            pol = Policy.model_validate(data)
            for r in pol.rules:
                if roster:
                    keys = []
                    for o in r.owners:
                        k = roster.resolve(o)
                        if k is None:
                            raise ValueError(f"owner '{o}' is not in the roster")
                        keys.append(k)
                    r.owners = keys
                if not r.owners:
                    raise ValueError(f"rule '{r.match}' needs owners")
                if r.quorum is not None and not 1 <= r.quorum <= len(r.owners):
                    raise ValueError(f"rule '{r.match}': quorum must be between 1 and {len(r.owners)}")
            return pol

        return await self._json_call(system, user, parse)


def canned_plan(task: str, roster: Roster, policy: Policy) -> Plan:
    """Offline plan for --dry-run without an API key. Not used for live runs."""
    keys = [k for k in roster.people if k != roster.requester] or list(roster.people)
    first = keys[:2]
    last = keys[-1]
    ds = [
        PlanDecision(id="d1", question=f"Approve going ahead with: {task[:300]}?", owners=first,
                     quorum=len(first), expires_s=policy.default_expires_s),
        PlanDecision(id="d2", question="Approve the transport and timing for this plan?", owners=[last],
                     quorum=1, depends_on=["d1"], expires_s=policy.default_expires_s),
        PlanDecision(id="d3", question="Approve booking everything now?", owners=first, quorum=1,
                     depends_on=["d1", "d2"], expires_s=policy.default_expires_s),
    ]
    return validate_plan(Plan(decisions=ds), roster, policy)
