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
