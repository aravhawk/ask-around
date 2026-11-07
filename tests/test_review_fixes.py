import asyncio
import json
import os

import httpx
import pytest
from rich.console import Console

from askaround.board import Board
from askaround.cli import main
from askaround.engine import Engine
from askaround.fake import FakeNotify
from askaround.models import Person, Plan, PlanDecision, Policy, Roster, load_policy, load_roster, validate_plan
from askaround.notify import Notify
from askaround.planner import Kai, PlannerError
from askaround.runner import run_task
from conftest import build, dec, make_roster, scripted

ROOT = os.path.join(os.path.dirname(__file__), "..")


# ---- 2: require_all_owners must include rule-added owners
def test_require_all_owners_counts_owners_added_by_rules():
    ro = Roster(people={k: Person(name=k, webhook="https://x/" + k) for k in "abc"})
    po = Policy.model_validate({"require_all_owners": True, "rules": [{"match": "hotel", "owners": ["c"], "quorum": 1}]})
    d = PlanDecision(id="hotel", question="Hotel?", owners=["a", "b"], quorum=1)
    validate_plan(Plan(decisions=[d]), ro, po)
    assert d.owners == ["a", "b", "c"] and d.quorum == 3


# ---- 3: typo'd rule owners fail closed
def test_rule_owner_typo_fails_closed_everywhere():
    ro = make_roster()
    po = Policy.model_validate({"rules": [{"match": "hotel", "owners": ["typo"], "quorum": 1}]})
    with pytest.raises(ValueError, match="typo"):
        po.bind(ro)
    with pytest.raises(ValueError, match="typo"):
        validate_plan(Plan(decisions=[PlanDecision(id="hotel", question="hotel?", owners=["mom"])]), ro, po)
    with pytest.raises(ValueError):
        Policy.model_validate({"rules": [{"match": "x", "owners": ["mom"], "quorum": 2}]}).bind(ro)
    ok = Policy.model_validate({"rules": [{"match": "x", "owners": ["Mom", "DAD"], "quorum": 2}]}).bind(ro)
    assert ok.rules[0].owners == ["mom", "dad"]


# ---- 4: a missing policy file is an error, never silently ungoverned
async def test_missing_policy_path_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    with pytest.raises(FileNotFoundError):
        load_policy(str(tmp_path / "polcy.yaml"))
    with pytest.raises(FileNotFoundError):
        await run_task("t", os.path.join(ROOT, "roster.example.yaml"), str(tmp_path / "polcy.yaml"), dry_run=True,
                       show_board=False, console=Console(quiet=True))
    rc = await asyncio.to_thread(main, ["t", "--roster", os.path.join(ROOT, "roster.example.yaml"),
                                        "--policy", str(tmp_path / "polcy.yaml"), "--dry-run"])
    assert rc == 2
    from askaround.mcp_server import ask_around_impl
    r = await ask_around_impl("t", os.path.join(ROOT, "roster.example.yaml"), str(tmp_path / "polcy.yaml"))
    assert r["outcome"] == "error" and "policy not found" in r["error"]


async def test_no_policy_given_and_none_in_cwd_runs_with_visible_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    c = Console(record=True, width=200)
    await run_task("t", os.path.join(ROOT, "roster.example.yaml"), None, dry_run=True, show_board=False, console=c, speed=0.05)
    assert "NO policy rules" in c.export_text()


def test_bad_yaml_is_a_value_error_and_cli_exit_2(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("people: [unclosed")
    with pytest.raises(ValueError):
        load_roster(bad)
    assert main(["t", "--roster", str(bad), "--dry-run"]) == 2


# ---- 6: escalation asks are not votes and don't clobber waiting_on
async def test_non_owner_backup_gets_no_vote_entry():
    ro = Roster(requester="me", people={
        "me": Person(name="Me", webhook="https://fake/me"),
        "mom": Person(name="Mom", webhook="https://fake/mom", backup="me")})
    eng, _ = build([dec("d1", ["mom"])], scripted({("mom", 1): [(0, "expired")], ("me", 1): [(0, "yes")]}), roster=ro)
    r = await eng.run()
    assert r["outcome"] == "approved"
    assert set(r["decisions"][0]["votes"]) == {"mom"}  # no phantom permanent "pending" for the backup


async def test_backup_who_is_also_an_owner_keeps_their_primary_countdown():
    eng, _ = build([dec("d1", ["mom", "dad"], 2)], scripted({
        ("mom", 1): [(0.05, "expired")], ("dad", 1): [(0.5, "approved")], ("dad", 2): [(0.15, "yes")]}))
    seen = {}

    async def sample():
        await asyncio.sleep(0.12)   # escalation to dad is open AND dad's primary is open
        seen["both"] = sorted(a.kind for a in eng.states["d1"].waiting_on.values() if a.person == "dad")
        await asyncio.sleep(0.2)    # escalation settled; dad's primary must still be tracked
        seen["after"] = sorted(a.kind for a in eng.states["d1"].waiting_on.values() if a.person == "dad")
        seen["live"] = eng._live_text()[0]

    t = asyncio.create_task(sample())
    r = await eng.run()
    await t
    assert seen["both"] == ["escalation", "primary"]
    assert seen["after"] == ["primary"] and "waiting on Dad" in seen["live"]
    assert r["outcome"] == "approved"


# ---- 7: hostile strings can never crash the board / CLI
HOSTILE = "Pay deposit [/urgent] [bold]x[/bold] [red"


def test_board_survives_markup_in_every_string(tmp_path):
    ro = Roster(requester="me", people={"me": Person(name="Me [/x]", webhook="https://fake/me"),
                                        "mom": Person(name="Mom [/y]", webhook="https://fake/mom")})
    eng = Engine(HOSTILE, Plan(decisions=[PlanDecision(id="d[/1]", question=HOSTILE, owners=["mom"], expires_s=30)]),
                 ro, Policy(), Notify(httpx.AsyncClient()))
    d = eng.states["d[/1]"]
    d.reason = HOSTILE
    d.status = "failed"
    eng.log.append(HOSTILE)
    eng.replans.append({"summary": HOSTILE})
    c = Console(record=True, width=160)
    b = Board(eng, c)
    c.print(b.render())
    receipt = eng.receipt()
    receipt["decisions"][0]["reason"] = HOSTILE
    b.summary(receipt, "x[/y].json")
    assert "Pay deposit" in c.export_text()


def test_cli_error_text_with_brackets_does_not_crash(tmp_path):
    r = tmp_path / "r.yaml"
    r.write_text("people:\n  a: {role: [1]}\n")  # pydantic error text contains "[type=...]"
    assert main(["t", "--roster", str(r), "--dry-run"]) == 2


async def test_board_failure_never_changes_the_outcome():
    eng, _ = build([dec("d1", ["mom"])], scripted({}))
    eng.on_change = lambda: (_ for _ in ()).throw(RuntimeError("board exploded"))
    assert (await eng.run())["outcome"] == "approved"


# ---- 8: Kai transport errors become PlannerError
async def test_kai_transport_errors_are_planner_errors():
    def boom(req):
        raise httpx.ReadTimeout("slow")
    kai = Kai(httpx.AsyncClient(transport=httpx.MockTransport(boom)), api_key="k")
    with pytest.raises(PlannerError):
        await kai.plan("x", make_roster(), Policy())


def test_cli_exits_2_when_planner_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("BELWEAVE_API_KEY", "k")
    import askaround.planner as pl

    class Boom(httpx.AsyncClient):
        async def post(self, *a, **k):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(pl.httpx, "AsyncClient", Boom)
    rc = main(["t", "--roster", os.path.join(ROOT, "roster.example.yaml"), "--policy", os.path.join(ROOT, "policy.yaml")])
    assert rc == 2


# ---- 10: cancelled while the ask POST is in flight -> the prompt is still cancelled
async def test_veto_during_inflight_ask_cancels_the_delivered_prompt():
    fake = FakeNotify(scripted({("dad", 1): [(0, "denied")], ("mom", 1): [(60, "approved")]}))

    async def handler(req: httpx.Request):
        if req.method == "POST" and req.url.path.strip("/") == "mom":
            await asyncio.sleep(0.4)  # mom's ask is still in flight when dad vetoes
        return fake.handle(req)

    eng, _ = build([dec("d1", ["mom", "dad"], 1)], scripted({}))
    eng.notify = Notify(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await eng.run()
    assert r["decisions"][0]["outcome"] == "failed"
    mom = [a for a in r["asks"] if a["person"] == "mom"][0]
    assert mom["eventId"] and mom["status"] == "canceled"
    assert any(m == "POST" and u.endswith(f"/events/{mom['eventId']}/cancel") for m, u, *_ in fake.requests)
