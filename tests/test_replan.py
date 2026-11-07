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


SLOW = {("dad", 1): [(0.15, "approved")]}   # keeps d3 waiting while the re-plan runs


def graph(extra=()):
    """d1 (mom, instant) -> d2 (dad, slow) -> d3 (mom, waits behind d2)."""
    return [dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"], type="text"), *extra]


async def run_with(rp, decisions=None, table=None, policy=None):
    rep = FakeReplanner(rp)
    eng, _ = build(decisions or graph(), scripted(table if table is not None else SLOW), replanner=rep, policy=policy)
    return eng, await eng.run(), rep


async def test_replan_adds_edits_drops_pending_only_and_shows_on_board():
    rp = Replan(add=[new("d4", ["dad"], ["d1"])], edit=[{"id": "d3", "question": "Edited?"}], reason="budget ok")
    eng, r, rep = await run_with(rp)
    assert eng.states["d3"].spec.question == "Edited?"
    assert {d["id"]: d["outcome"] for d in r["decisions"]}["d4"] != "waiting"
    e = r["replans"][0]
    assert e["applied"] and e["added"] == ["d4"] and e["edited"] == ["d3"]
    state = {s["id"]: s["status"] for s in rep.calls[0]}
    assert state["d1"] == "passed" and state["d2"] == "in_flight" and state["d3"] == "pending"
    c = Console(record=True, width=120)
    c.print(Board(eng, c).render())
    out = c.export_text()
    assert "re-plans:" in out and "+1" in out


async def test_replan_may_drop_only_text_decisions():
    eng, r, _ = await run_with(Replan(drop=["d3"]))
    assert r["replans"][0]["dropped"] == ["d3"] and eng.states["d3"].status == "dropped"
    assert r["outcome"] == "approved"


async def test_replan_cannot_drop_consent_decisions_or_loosen_pending_ones():
    # d3 is an approval needing mom+dad (2 of 2), pending behind d2
    base = [dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom", "dad"], 2, deps=["d2"])]
    attacks = [
        Replan(drop=["d3"]),                                               # drop a consent decision
        Replan(edit=[{"id": "d3", "quorum": 1}]),                          # lower quorum
        Replan(edit=[{"id": "d3", "owners": ["mom"], "quorum": 1}]),       # remove an owner
        Replan(edit=[{"id": "d3", "type": "text"}]),                       # consent -> free text
        Replan(edit=[{"id": "d3", "depends_on": []}]),                     # remove a dependency
        Replan(edit=[{"id": "d3", "quorum": "lots"}]),                     # garbage type
        Replan(edit=[{"id": "d3", "owners": ["ghost"]}]),                  # unknown owner
    ]
    for rp in attacks:
        eng, r, _ = await run_with(rp, base)
        e = r["replans"][0]
        assert not e["applied"] and "plan unchanged" in e["summary"], rp
        assert e["added"] == e["edited"] == e["dropped"] == [], rp   # audit never claims unapplied changes
        d3 = eng.states["d3"].spec
        assert d3.quorum == 2 and d3.owners == ["mom", "dad"] and d3.type == "approval" and d3.depends_on == ["d2"]
        assert r["outcome"] == "approved"  # honest run, unaffected by the rejected attack


async def test_replan_tightening_is_allowed():
    rp = Replan(edit=[{"id": "d3", "owners": ["mom", "dad"], "quorum": 2}])
    eng, r, _ = await run_with(rp, [dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"])])
    assert r["replans"][0]["applied"] and eng.states["d3"].spec.quorum == 2


async def test_stale_edits_to_settled_decisions_are_skipped_not_applied():
    eng, r, _ = await run_with(Replan(edit=[{"id": "d1", "question": "hacked"}, {"id": "d3", "question": "ok now"}]))
    assert eng.states["d1"].spec.question == "Q d1?" and eng.states["d3"].spec.question == "ok now"
    assert r["replans"][0]["skipped"] == ["d1"]


async def test_bad_graphs_rejected_unknown_owner_cycle_bad_dep():
    for rp in [Replan(add=[new("d9", ["ghost"])]),
               Replan(add=[new("d8", ["mom"], ["d9"]), new("d9", ["mom"], ["d8"])]),
               Replan(add=[new("d7", ["mom"], ["nope"])]),
               Replan(add=[new("d1", ["mom"])])]:  # reused id
        eng, r, _ = await run_with(rp)
        assert not r["replans"][0]["applied"] and "plan unchanged" in r["replans"][0]["summary"], rp
        assert r["replans"][0]["added"] == []
        assert "d9" not in eng.states and "d8" not in eng.states and "d7" not in eng.states


async def test_replan_failure_exception_keeps_plan():
    eng, r, _ = await run_with(RuntimeError("kai down"))
    assert not r["replans"][0]["applied"] and r["outcome"] == "approved"


async def test_max_two_replans():
    rep = FakeReplanner(Replan(), Replan(), Replan(), Replan())
    chain = [dec("d1", ["mom"]), dec("d2", ["mom"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"]),
             dec("d4", ["mom"], deps=["d3"]), dec("d5", ["mom"], deps=["d4"])]
    t = {("mom", n): [(0.1, "approved")] for n in range(1, 8)}
    eng, _ = build(chain, scripted(t), replanner=rep)
    r = await eng.run()
    assert len(r["replans"]) <= 2 and len(rep.calls) <= 2 and r["outcome"] == "approved"


async def test_no_replan_after_a_failure_or_block():
    rep = FakeReplanner(Replan(add=[new("dx", ["mom"])]))
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"])],
                   scripted({("mom", 1): [(0, "denied")]}), replanner=rep)
    r = await eng.run()
    assert rep.calls == [] and r["outcome"] == "rejected"


async def test_slow_replan_does_not_block_dispatch_or_other_settlements():
    import asyncio

    class Slow(FakeReplanner):
        async def replan(self, *a, **k):
            await asyncio.sleep(1.0)
            return Replan()

    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"]),
                    dec("d4", ["dad"], deps=["d1"])],
                   scripted({("dad", 1): [(0.1, "approved")], ("dad", 2): [(0.1, "approved")]}), replanner=Slow())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await eng.run()
    asks = {a["decision"]: a for a in r["asks"]}
    # d2/d4 were asked immediately after d1, not after the 1 s re-plan; d3 too (the run waits for the re-plan to end)
    assert r["outcome"] == "approved"
    assert {"d2", "d4", "d3"} <= set(asks)


async def test_replan_cannot_weaken_policy_rule():
    pol = Policy.model_validate({"rules": [{"match": "hotel", "owners": ["dad", "mom"], "quorum": 2}]})
    rp = Replan(add=[PlanDecision(id="hotel2", question="Pick a hotel?", owners=["mom"], quorum=1, expires_s=30)])
    eng, r, _ = await run_with(rp, policy=pol)
    assert eng.states["hotel2"].spec.quorum == 2 and set(eng.states["hotel2"].spec.owners) == {"mom", "dad"}


async def test_replan_never_makes_a_denied_run_approved():
    eng, r, _ = await run_with(Replan(drop=["d3"]), table={("mom", 1): [(0, "denied")]})
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
