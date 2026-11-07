import json

import httpx
import pytest
from rich.console import Console

from askaround.board import Board
from askaround.planner import Kai, PlannerError, Replan, _extract_json
from askaround.models import Policy, PlanDecision
from conftest import build, dec, make_roster, scripted


class FakeReplanner:
    def __init__(self, *replans):
        self.replans, self.calls = list(replans), []

    async def replan(self, task, state, roster, policy, existing_ids):
        self.calls.append(state)
        r = self.replans.pop(0) if self.replans else Replan()
        if isinstance(r, Exception):
            raise r
        return r


def new(id, owners, deps=()):
    return PlanDecision(id=id, question=f"Q {id}?", owners=owners, quorum=1, depends_on=list(deps), expires_s=30)


async def test_replan_adds_edits_drops_pending_only_and_shows_on_board():
    rp = Replan(add=[new("d4", ["dad"], ["d1"])], edit=[{"id": "d2", "question": "Edited?"}], drop=["d3"], reason="budget ok")
    rep = FakeReplanner(rp)
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d1"])], scripted({}),
                   replanner=rep)
    r = await eng.run()
    outs = {d["id"]: d["outcome"] for d in r["decisions"]}
    assert outs["d4"] == "passed" and outs["d3"] == "dropped" and outs["d2"] == "passed"
    assert eng.states["d2"].spec.question == "Edited?"
    assert r["replans"][0]["added"] == ["d4"] and r["replans"][0]["dropped"] == ["d3"]
    assert r["outcome"] == "approved"  # dropped decisions don't count; everything remaining passed
    state = rep.calls[0]
    assert {s["id"]: s["status"] for s in state}["d1"] == "passed"
    assert {s["id"]: s["status"] for s in state}["d2"] == "pending"
    c = Console(record=True, width=120)
    c.print(Board(eng, c).render())
    out = c.export_text()
    assert "re-plans:" in out and "+1" in out


async def test_replan_cannot_edit_settled_or_inflight_and_bad_replan_is_ignored():
    bad = Replan(edit=[{"id": "d1", "question": "hacked"}])
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"])], scripted({}), replanner=FakeReplanner(bad))
    r = await eng.run()
    assert eng.states["d1"].spec.question == "Q d1?"
    assert "plan unchanged" in r["replans"][0]["summary"]
    assert r["outcome"] == "approved"


async def test_replan_validation_rejects_unknown_owner_quorum_cycle_and_dropped_dependency():
    for rp in [Replan(add=[new("d9", ["ghost"])]),
               Replan(edit=[{"id": "d2", "quorum": 5}]),
               Replan(edit=[{"id": "d2", "depends_on": ["d3"]}, {"id": "d3", "depends_on": ["d2"]}]),
               Replan(drop=["d2"])]:  # d3 depends on d2 -> dropping would orphan it
        eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["dad"], deps=["d2"])],
                       scripted({}), replanner=FakeReplanner(rp))
        r = await eng.run()
        assert "plan unchanged" in r["replans"][0]["summary"], rp
        assert [d["outcome"] for d in r["decisions"]] == ["passed"] * 3


async def test_max_two_replans_and_failure_counts():
    rep = FakeReplanner(Replan(), RuntimeError("kai down"), Replan())
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["mom"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"]),
                    dec("d4", ["mom"], deps=["d3"])], scripted({}), replanner=rep)
    r = await eng.run()
    assert len(r["replans"]) == 2 and len(rep.calls) == 2
    assert r["outcome"] == "approved"


async def test_replan_cannot_weaken_policy_rule():
    pol = Policy.model_validate({"rules": [{"match": "hotel", "owners": ["dad", "mom"], "quorum": 2}]})
    rp = Replan(add=[PlanDecision(id="hotel2", question="Pick a hotel?", owners=["mom"], quorum=1, expires_s=30)])
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["mom"], deps=["d1"])], scripted({}), replanner=FakeReplanner(rp), policy=pol)
    await eng.run()
    assert eng.states["hotel2"].spec.quorum == 2 and set(eng.states["hotel2"].spec.owners) == {"mom", "dad"}


async def test_replan_never_makes_a_denied_run_approved():
    rp = Replan(drop=["d2"])
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"])], scripted({("mom", 1): [(0, "denied")]}),
                   replanner=FakeReplanner(rp))
    r = await eng.run()
    assert r["outcome"] == "rejected"


# ---------- Kai planner parsing ----------
def kai_with(*contents):
    it = iter(contents)

    def h(req):
        c = next(it)
        return httpx.Response(200, json={"choices": [{"message": {"content": c}}]})

    return Kai(httpx.AsyncClient(transport=httpx.MockTransport(h)), api_key="test-key"), h


def test_extract_json_strips_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure! {"a": 1} done') == {"a": 1}


async def test_kai_plan_retries_once_on_parse_failure_and_sends_max_tokens_and_no_webhooks():
    seen = []
    good = json.dumps({"decisions": [{"id": "d1", "question": "ok?", "type": "approval", "owners": ["Mom"], "quorum": 1,
                                      "depends_on": [], "expires_s": 30}]})
    contents = iter(["", "```json\n" + good + "\n```"])

    def h(req):
        seen.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": next(contents)}}]})

    kai = Kai(httpx.AsyncClient(transport=httpx.MockTransport(h)), api_key="test-key")
    plan = await kai.plan("do it", make_roster(), Policy())
    assert plan.decisions[0].owners == ["mom"] and len(seen) == 2
    assert seen[0]["model"] == "Belweave/Kai-4" and seen[0]["max_tokens"] >= 2000
    assert "fake/mom" not in json.dumps(seen)  # webhook URLs never leave the machine


async def test_kai_plan_fails_after_two_bad_outputs_and_rejects_bad_owners():
    kai, _ = kai_with("nope", "still nope")
    with pytest.raises(PlannerError):
        await kai.plan("x", make_roster(), Policy())
    bad = json.dumps({"decisions": [{"id": "d1", "question": "q", "owners": ["stranger"], "quorum": 1}]})
    kai, _ = kai_with(bad, bad)
    with pytest.raises(PlannerError):
        await kai.plan("x", make_roster(), Policy())


async def test_kai_requires_api_key(monkeypatch):
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    with pytest.raises(PlannerError):
        await Kai(httpx.AsyncClient()).plan("x", make_roster(), Policy())
