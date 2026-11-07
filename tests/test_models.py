import json
import pytest

from askaround.models import Plan, PlanDecision, Policy, Roster, load_policy, load_roster, validate_plan
from conftest import build, dec, make_roster, scripted


def test_prd_roster_format_loads(tmp_path):
    f = tmp_path / "r.yaml"
    f.write_text("""
requester: arav
people:
  arav: { role: "requester", webhook: "https://x/a", backup: null }
  dad:  { role: "parent", webhook: "https://x/d", backup: arav }
""")
    r = load_roster(f)
    assert r.people["dad"].webhook_url == "https://x/d" and r.people["dad"].name == "Dad"
    assert "https://x" not in repr(r)  # secrets never in repr


def test_legacy_roster_keys_still_work():
    r = Roster.model_validate({"people": {"a": {"name": "A", "webhook_url": "https://x/a"}}})
    assert r.people["a"].webhook_url == "https://x/a"


def test_prd_policy_format_and_legacy_policy():
    p = Policy.model_validate({"rules": [{"match": "money > 200", "owners": ["mom", "dad"], "quorum": 2}],
                               "defaults": {"expires_s": 30, "veto_on_deny": True, "max_reasks": 1, "max_replans": 2}})
    assert p.default_expires_s == 30 and p.rules[0].quorum == 2
    legacy = Policy.model_validate({"max_reasks": 2, "default_expires_s": 60, "rules": ["be careful"]})
    assert legacy.rules[0].match == "be careful" and legacy.max_reasks == 2
    assert Policy.model_validate({"defaults": {"max_replans": 2}}).max_replans == 2
    assert Policy.model_validate({"defaults": {"max_replans": 9}}).max_replans == 2  # clamped, not rejected
    assert Policy.model_validate({"max_replans": 0}).max_replans == 0


def test_validate_plan_rejects_bad_plans():
    ro, po = make_roster(), Policy()
    with pytest.raises(ValueError):
        validate_plan(Plan(decisions=[dec("d1", ["ghost"])]), ro, po)
    with pytest.raises(ValueError):
        validate_plan(Plan(decisions=[PlanDecision(id="d1", question="q", owners=["mom"], quorum=2)]), ro, po)
    with pytest.raises(ValueError):
        validate_plan(Plan(decisions=[dec("d1", ["mom"], deps=["d2"]), dec("d2", ["dad"], deps=["d1"])]), ro, po)
    with pytest.raises(ValueError):
        validate_plan(Plan(decisions=[dec("d1", ["mom"], deps=["nope"])]), ro, po)


def test_keyword_rule_enforced_after_planning_but_expression_rule_not():
    ro = make_roster()
    po = Policy.model_validate({"rules": [{"match": "hotel", "owners": ["dad", "mom"], "quorum": 2},
                                          {"match": "money > 200", "owners": ["mom"], "quorum": 1}]})
    d = PlanDecision(id="hotel", question="Which hotel?", owners=["mom"], quorum=1)
    other = PlanDecision(id="x", question="Spend $500?", owners=["dad"], quorum=1)
    validate_plan(Plan(decisions=[d, other]), ro, po)
    assert set(d.owners) == {"mom", "dad"} and d.quorum == 2
    assert other.owners == ["dad"]  # expression rules are the planner's job


def test_require_all_owners():
    d = dec("d1", ["mom", "dad"], 1)
    validate_plan(Plan(decisions=[d]), make_roster(), Policy(require_all_owners=True))
    assert d.quorum == 2


async def test_text_reply_satisfies_text_decision_only():
    eng, _ = build([dec("h", ["dad"], type="text")], scripted({("dad", 1): [(0, "replied_empty")]}))
    # no text in the fake reply -> empty reply is NOT an answer
    r = await eng.run()
    assert r["outcome"] != "approved"


async def test_text_reply_with_text_passes_text_decision():
    eng, _ = build([dec("h", ["dad"], type="text")], scripted({}))
    r = await eng.run()
    assert r["outcome"] == "approved" and r["asks"][0]["text"] == "Under $150, walkable"


async def test_text_reply_with_text_passes_but_replied_never_approves_yes_no(monkeypatch):
    from askaround.notify import is_answer
    assert is_answer("replied", "text", "Under $150") and not is_answer("replied", "text", "  ")
    assert not is_answer("replied", "yes_no", "yes") and not is_answer("replied", "approval", "approved")
    assert not is_answer("denied", "text", "x") and not is_answer("expired", "text", "x")


async def test_veto_on_deny_false_does_not_veto_if_quorum_still_reachable():
    eng, _ = build([dec("d1", ["mom", "dad"], 1)], scripted({("mom", 1): [(0, "denied")], ("dad", 1): [(0.05, "approved")]}),
                   policy=Policy(veto_on_deny=False))
    r = await eng.run()
    assert r["decisions"][0]["outcome"] == "passed"


async def test_wrong_person_answer_is_ignored_and_reasked():
    ro = make_roster(mom="u_mom")
    calls = {"n": 0}

    def script(person, nth, payload):
        return [(0, "approved")]

    eng, fake = build([dec("d1", ["mom"])], script, roster=ro, user_ids={"mom": "u_stranger"})
    r = await eng.run()
    assert r["outcome"] != "approved"          # a stranger can never approve for Mom
    assert [a["status"] for a in r["asks"] if a["person"] == "mom"].count("ignored_wrong_responder") >= 1
    assert len([a for a in r["asks"] if a["person"] == "mom"]) >= 2  # re-asked

    eng, fake = build([dec("d1", ["mom"])], script, roster=ro, user_ids={"mom": "u_mom"})
    assert (await eng.run())["outcome"] == "approved"


# ---- max_expires_s / --expires
def test_max_expires_clamps_planner_copied_values_including_replanned_decisions():
    po = Policy.model_validate({"defaults": {"expires_s": 30, "max_expires_s": 30}})
    ds = [PlanDecision(id="a", question="q", owners=["mom"], expires_s=300),
          PlanDecision(id="b", question="q", owners=["mom"], expires_s=86400),
          PlanDecision(id="c", question="q", owners=["mom"], expires_s=40),
          PlanDecision(id="d", question="q", owners=["mom"])]
    validate_plan(Plan(decisions=ds), make_roster(), po)
    assert [d.expires_s for d in ds] == [30, 30, 30, 30]
    assert Policy().max_expires_s is None  # default: no clamp
    free = [PlanDecision(id="a", question="q", owners=["mom"], expires_s=300)]
    validate_plan(Plan(decisions=free), make_roster(), Policy())
    assert free[0].expires_s == 300
    with pytest.raises(Exception):
        Policy.model_validate({"max_expires_s": 5})


def test_shipped_policy_yaml_clamps_to_30(tmp_path):
    import os
    po = load_policy(os.path.join(os.path.dirname(__file__), "..", "policy.yaml"))
    assert po.max_expires_s == 30
    d = PlanDecision(id="a", question="q", owners=["mom"], expires_s=300)
    validate_plan(Plan(decisions=[d]), make_roster(), po)
    assert d.expires_s == 30


def test_planner_prompt_example_no_longer_teaches_300():
    from askaround.planner import PLAN_SYSTEM
    assert '"expires_s":300' not in PLAN_SYSTEM and '"expires_s":30}' in PLAN_SYSTEM


async def test_kai_copying_300_ends_up_at_policy_max():
    import httpx
    from askaround.planner import Kai
    bad = json.dumps({"decisions": [{"id": "d1", "question": "ok?", "owners": ["mom"], "quorum": 1, "expires_s": 300}]})
    kai = Kai(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": bad}}]}))), api_key="k")
    plan = await kai.plan("x", make_roster(), Policy.model_validate({"defaults": {"max_expires_s": 30}}))
    assert plan.decisions[0].expires_s == 30


async def test_replanned_decisions_are_clamped_too():
    from askaround.planner import Replan
    from test_replan import FakeReplanner
    rp = Replan(add=[PlanDecision(id="n", question="q", owners=["mom"], expires_s=600)])
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["mom"], deps=["d2"])],
                   scripted({("dad", 1): [(0.15, "approved")]}), replanner=FakeReplanner(rp),
                   policy=Policy.model_validate({"defaults": {"max_expires_s": 30}}))
    await eng.run()
    assert eng.states["n"].spec.expires_s == 30


async def test_cli_expires_overrides_every_decision(tmp_path, monkeypatch):
    import os
    from rich.console import Console
    from askaround.runner import run_task
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    root = os.path.join(os.path.dirname(__file__), "..")
    r = await run_task("t", os.path.join(root, "roster.example.yaml"), os.path.join(root, "policy.yaml"), dry_run=True,
                       show_board=False, console=Console(quiet=True), speed=0.05, expires=45)
    assert {d["expires_s"] for d in r["plan"]["decisions"]} == {45}
    from askaround.cli import _run_parser
    assert _run_parser().parse_args(["t", "--expires", "45"]).expires == 45
    rc = await __import__("asyncio").to_thread(__import__("askaround.cli", fromlist=["main"]).main,
                                               ["t", "--roster", os.path.join(root, "roster.example.yaml"), "--dry-run", "--expires", "5"])
    assert rc == 2  # below Notify's 30 s minimum -> error, not a silent clamp
