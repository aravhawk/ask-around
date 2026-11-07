import httpx
import pytest

from askaround.engine import Engine
from askaround.fake import FakeNotify
from askaround.models import Person, Plan, PlanDecision, Policy, Roster
from askaround.notify import Notify


def make_roster(**user_ids):
    return Roster(requester="me", people={
        "me": Person(name="Me", role="requester", webhook_url="https://fake/me"),
        "mom": Person(name="Mom", role="parent", webhook_url="https://fake/mom", backup="dad", user_id=user_ids.get("mom")),
        "dad": Person(name="Dad", role="parent", webhook_url="https://fake/dad", backup="mom", user_id=user_ids.get("dad")),
    })


def dec(id, owners, quorum=1, deps=(), type="approval"):
    return PlanDecision(id=id, question=f"Q {id}?", type=type, owners=list(owners), quorum=quorum,
                        depends_on=list(deps), expires_s=30)


def build(decisions, script, roster=None, policy=None, user_ids=None, **kw):
    roster = roster or make_roster()
    fake = FakeNotify(script, user_ids=user_ids)
    http = httpx.AsyncClient(transport=fake.transport())
    eng = Engine("test task", Plan(decisions=decisions), roster, policy or Policy(), Notify(http),
                 poll_interval=0.01, **kw)
    return eng, fake


def scripted(table):
    """table: {(person, nth): [(delay, status)...]}; default approves instantly."""
    def s(person, nth, payload):
        t = table.get((person, nth))
        if t is not None:
            return t
        return [(0, "approved" if payload["response"]["type"] == "approval" else "yes")]
    return s
