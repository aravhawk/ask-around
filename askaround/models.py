"""Config + plan models. Webhook URLs are secrets: never repr/log them."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class Person(BaseModel):
    """PRD section 7 uses `webhook:`; `webhook_url:` is accepted too (backward compatible)."""
    key: str = ""
    name: str = ""
    role: str = "person"
    webhook_url: str = Field(repr=False, validation_alias=AliasChoices("webhook", "webhook_url"))
    backup: str | None = None
    user_id: str | None = None


class Roster(BaseModel):
    requester: str | None = None
    people: dict[str, Person]

    @model_validator(mode="after")
    def _check(self) -> "Roster":
        for k, p in self.people.items():
            p.key = k
            p.name = p.name or k.title()
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


class Rule(BaseModel):
    """PRD section 7 rule. `match` is applied by the planner; plain keywords are also enforced deterministically."""
    match: str
    owners: list[str] = Field(default_factory=list)
    quorum: int | None = None


_OPERATOR = re.compile(r"[<>=!$]|\bover\b|\bunder\b|\bmore than\b|\bless than\b")


class Policy(BaseModel):
    """Accepts the PRD shape (`rules:` of objects + `defaults:`) and the older flat keys / string rules."""
    max_reasks: int = Field(default=1, ge=0, le=5)
    default_expires_s: int = Field(default=300, ge=30, le=86400)
    max_expires_s: int | None = Field(default=None, ge=30, le=86400)   # clamp every decision to at most this
    force_expires_s: int | None = Field(default=None, ge=30, le=86400, exclude=True)  # CLI --expires: runtime override
    veto_on_deny: bool = True
    require_all_owners: bool = False
    max_replans: int = Field(default=2, ge=0)  # clamped to 2 (PRD F8) by the validator below
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        merged = dict(data.pop("defaults", None) or {})
        if "expires_s" in merged:
            merged["default_expires_s"] = merged.pop("expires_s")
        if "expires_s" in data:
            data["default_expires_s"] = data.pop("expires_s")
        for k, v in merged.items():
            data.setdefault(k, v)
        data["rules"] = [{"match": r} if isinstance(r, str) else r for r in (data.get("rules") or [])]
        return data

    @field_validator("max_replans", mode="after")
    @classmethod
    def _cap_replans(cls, v: int) -> int:
        return min(v, 2)

    def bind(self, roster: "Roster") -> "Policy":
        """Fail closed: every rule owner must exist in the roster (normalised to roster keys) and every
        quorum must be satisfiable. Without this, a typo would silently disable a rule."""
        for r in self.rules:
            keys: list[str] = []
            for o in r.owners:
                k = roster.resolve(o)
                if k is None:
                    raise ValueError(f"policy rule '{r.match}': owner '{o}' is not in the roster")
                if k not in keys:
                    keys.append(k)
            r.owners = keys
            if r.quorum is not None and not (r.owners and 1 <= r.quorum <= len(r.owners)):
                raise ValueError(f"policy rule '{r.match}': quorum {r.quorum} needs 1..{len(r.owners)} owners")
        return self

    def rules_for_planner(self) -> list[dict]:
        return [r.model_dump(exclude_none=True) for r in self.rules]

    def as_yaml_dict(self) -> dict:
        return {"rules": self.rules_for_planner(),
                "defaults": {"expires_s": self.default_expires_s, "veto_on_deny": self.veto_on_deny,
                             "max_reasks": self.max_reasks, "max_replans": self.max_replans,
                             **({"max_expires_s": self.max_expires_s} if self.max_expires_s else {})}}


def enforce_rules(plan_decisions: list["PlanDecision"], roster: "Roster", policy: Policy) -> None:
    """Deterministic backstop: for rules whose `match` is a plain keyword (no operators), any decision whose
    id or question mentions it must include the rule's owners and meet its quorum. Expression rules
    (e.g. "money > 200") are left to the planner. Can only tighten, never loosen."""
    for r in policy.rules:
        if _OPERATOR.search(r.match.lower()) or not r.owners:
            continue
        kw = r.match.lower().strip()
        want = []
        for o in r.owners:
            k = roster.resolve(o)
            if k is None:  # never silently skip a rule
                raise ValueError(f"policy rule '{r.match}': owner '{o}' is not in the roster")
            want.append(k)
        for d in plan_decisions:
            if kw in d.id.lower() or kw in d.question.lower():
                for k in want:
                    if k not in d.owners:
                        d.owners.append(k)
                if r.quorum:
                    d.quorum = max(d.quorum, min(r.quorum, len(d.owners)))


class PlanDecision(BaseModel):
    id: str
    question: str
    type: Literal["approval", "yes_no", "text"] = "approval"
    owners: list[str]
    quorum: int = 1
    depends_on: list[str] = Field(default_factory=list)
    expires_s: int | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        return v if v in ("approval", "yes_no", "text") else "yes_no"

    @field_validator("expires_s", mode="before")
    @classmethod
    def _clamp(cls, v):
        if v is None:
            return None
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
    enforce_rules(plan.decisions, roster, policy)  # may add owners / raise quorum
    for d in plan.decisions:
        if policy.require_all_owners:
            d.quorum = len(d.owners)  # AFTER rules, so rule-added owners are included
        if not 1 <= d.quorum <= len(d.owners):
            raise ValueError(f"decision {d.id}: quorum {d.quorum} must be between 1 and {len(d.owners)}")
        d.expires_s = d.expires_s or policy.default_expires_s
        if policy.max_expires_s:  # the planner may copy any number; policy has the last word
            d.expires_s = min(d.expires_s, policy.max_expires_s)
        if policy.force_expires_s:
            d.expires_s = policy.force_expires_s
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


def _load_yaml(path: str | Path, what: str) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"{what} file {path} is not valid YAML") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{what} file {path} must contain a mapping")
    return data


def load_roster(path: str | Path) -> Roster:
    return Roster.model_validate(_load_yaml(path, "roster"))


def load_policy(path: str | Path | None) -> Policy:
    """No path -> built-in defaults. A path that does not exist is an ERROR (never silently ungoverned)."""
    if not path:
        return Policy()
    if not Path(path).is_file():
        raise FileNotFoundError(f"policy file not found: {path}")
    return Policy.model_validate(_load_yaml(path, "policy"))
