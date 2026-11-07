"""Config + plan models. Webhook URLs are secrets: never repr/log them."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Person(BaseModel):
    key: str = ""
    name: str
    role: str = "person"
    webhook_url: str = Field(repr=False)
    backup: str | None = None
    user_id: str | None = None


class Roster(BaseModel):
    requester: str | None = None
    people: dict[str, Person]

    @model_validator(mode="after")
    def _check(self) -> "Roster":
        for k, p in self.people.items():
            p.key = k
        for p in self.people.values():
            if p.backup and p.backup not in self.people:
                raise ValueError(f"{p.key}: backup '{p.backup}' is not in the roster")
        if self.requester and self.requester not in self.people:
            raise ValueError(f"requester '{self.requester}' is not in the roster")
        return self

    def resolve(self, ref: str) -> str | None:
        """Match a planner-supplied owner to a roster key (key or display name)."""
        r = ref.strip().lower()
        for k, p in self.people.items():
            if r == k.lower() or r == p.name.lower():
                return k
        return None

    def public_summary(self) -> list[dict]:
        """What the planner may see: no webhook URLs."""
        return [{"key": k, "name": p.name, "role": p.role, "backup": p.backup} for k, p in self.people.items()]


class Policy(BaseModel):
    max_reasks: int = Field(default=1, ge=0, le=5)
    default_expires_s: int = Field(default=300, ge=30, le=86400)
    require_all_owners: bool = False
    rules: list[str] = Field(default_factory=list)
    max_replans: int = Field(default=2, ge=0, le=5)


class PlanDecision(BaseModel):
    id: str
    question: str
    type: Literal["approval", "yes_no"] = "approval"
    owners: list[str]
    quorum: int = 1
    depends_on: list[str] = Field(default_factory=list)
    expires_s: int = 300

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        # Free text can never be consent; coerce anything unknown to a yes/no.
        return v if v in ("approval", "yes_no") else "yes_no"

    @field_validator("expires_s", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(30, min(86400, int(v)))
        except (TypeError, ValueError):
            return 300


class Plan(BaseModel):
    decisions: list[PlanDecision]


def validate_plan(plan: Plan, roster: Roster, policy: Policy, existing_ids: set[str] | None = None) -> Plan:
    """Normalise owners to roster keys; enforce quorum, dependency and uniqueness rules."""
    existing_ids = existing_ids or set()
    seen: set[str] = set()
    for d in plan.decisions:
        if not d.id or d.id in seen:
            raise ValueError(f"duplicate or empty decision id '{d.id}'")
        seen.add(d.id)
        keys = []
        for o in d.owners:
            k = roster.resolve(o)
            if k is None:
                raise ValueError(f"decision {d.id}: owner '{o}' is not in the roster")
            if k not in keys:
                keys.append(k)
        if not keys:
            raise ValueError(f"decision {d.id}: needs at least one owner")
        d.owners = keys
        if policy.require_all_owners:
            d.quorum = len(keys)
        if not 1 <= d.quorum <= len(keys):
            raise ValueError(f"decision {d.id}: quorum {d.quorum} must be between 1 and {len(keys)}")
        d.expires_s = d.expires_s or policy.default_expires_s
    known = seen | existing_ids
    for d in plan.decisions:
        for dep in d.depends_on:
            if dep not in known or dep == d.id:
                raise ValueError(f"decision {d.id}: bad dependency '{dep}'")
    # cycle check among the new decisions
    deps = {d.id: [x for x in d.depends_on if x in seen] for d in plan.decisions}
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        if state.get(n) == 1:
            raise ValueError("dependency cycle in plan")
        if state.get(n) == 2:
            return
        state[n] = 1
        for m in deps[n]:
            visit(m)
        state[n] = 2

    for n in deps:
        visit(n)
    return plan


def load_roster(path: str | Path) -> Roster:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Roster.model_validate(data)


def load_policy(path: str | Path | None) -> Policy:
    if not path or not Path(path).exists():
        return Policy()
    return Policy.model_validate(yaml.safe_load(Path(path).read_text()) or {})
