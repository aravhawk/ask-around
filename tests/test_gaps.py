"""Gap tests found by the coverage pass: Notify 429/400/malformed handling, CLI + subcommands, board, re-planner parsing."""
import asyncio
import json
import logging
import os

import httpx
import pytest
from rich.console import Console

from askaround.board import Board
from askaround.cli import main
from askaround.notify import Notify, NotifyError
from askaround.planner import Kai, PlannerError, Replan
from askaround.models import Policy
from conftest import build, dec, make_roster, scripted

ROOT = os.path.join(os.path.dirname(__file__), "..")
HOOK = "https://fake/mom"


def notify_with(handler, **kw):
    return Notify(httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


OK = {"ok": True, "eventId": "e1", "delivered": 1, "response": {"status": "pending", "expiresAt": "x"}}


async def ask(n, **kw):
    args = dict(title="t", body="b", type="approval", expires_s=30, correlation_id="c", idempotency_key="k")
    args.update(kw)
    return await n.ask(HOOK, **args)


# ---------------- Notify client ----------------
async def test_429_waits_retry_after_seconds_then_succeeds():
    calls, slept = [], []

    async def sleep(s):
        slept.append(s)

    def h(req):
        calls.append(1)
        return httpx.Response(429, json={"retryAfterSeconds": 7}) if len(calls) == 1 else httpx.Response(200, json=OK)

    eid, _ = await ask(notify_with(h, sleep=sleep))
    assert eid == "e1" and slept == [7.0] and len(calls) == 2


async def test_429_defaults_to_60s_and_gives_up_after_max_waits():
    slept = []

    async def sleep(s):
        slept.append(s)

    n = notify_with(lambda r: httpx.Response(429, json={}), sleep=sleep)
    with pytest.raises(NotifyError):
        await ask(n)
    assert slept == [60.0, 60.0, 60.0]


async def test_400_issues_are_logged_and_the_ask_fails(caplog):
    caplog.set_level(logging.ERROR)
    n = notify_with(lambda r: httpx.Response(400, json={"issues": [{"path": "response.expiresInSeconds", "message": "bad"}]}))
    with pytest.raises(NotifyError):
        await ask(n)
    assert "issues" in caplog.text and "expiresInSeconds" in caplog.text


async def test_ask_payload_limits_headers_and_clamps():
    seen = {}

    def h(req):
        seen["body"], seen["headers"] = json.loads(req.content), req.headers
        return httpx.Response(200, json=OK)

    await ask(notify_with(h), title="T" * 200, body="B" * 5000, expires_s=5, correlation_id="c" * 300, idempotency_key="i" * 500)
    b = seen["body"]
    assert len(b["title"]) == 80 and len(b["body"]) == 2000
    assert b["response"]["expiresInSeconds"] == 30 and len(b["response"]["correlationId"]) == 100
    assert len(seen["headers"]["idempotency-key"]) == 200 and "deviceIds" not in b
    await ask(notify_with(h), expires_s=10**9)
    assert seen["body"]["response"]["expiresInSeconds"] == 86400


async def test_bad_ask_responses_are_errors_not_asks():
    for resp in (httpx.Response(200, json={"ok": False}), httpx.Response(200, json={"ok": True}),
                 httpx.Response(500, json=OK), httpx.Response(200, text="not json")):
        with pytest.raises(NotifyError):
            await ask(notify_with(lambda r, resp=resp: resp))

    def boom(req):
        raise httpx.ConnectError("down")
    with pytest.raises(NotifyError):
        await ask(notify_with(boom))


async def test_cancel_swallows_404_and_network_errors():
    await notify_with(lambda r: httpx.Response(404, json={"ok": False})).cancel(HOOK, "e1")

    def boom(req):
        raise httpx.ConnectError("down")
    await notify_with(boom).cancel(HOOK, "e1")


async def test_read_http_error_is_notify_error():
    with pytest.raises(NotifyError):
        await notify_with(lambda r: httpx.Response(500, json={})).read(HOOK, "e1")


# ---------------- CLI + subcommands ----------------
@pytest.fixture
def dry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    return ["t", "--roster", os.path.join(ROOT, "roster.example.yaml"), "--policy", os.path.join(ROOT, "policy.yaml"),
            "--dry-run", "--speed", "0.05"]


async def test_cli_exit_codes_for_approved_and_rejected(dry, tmp_path):
    assert await asyncio.to_thread(main, dry) == 0
    assert json.loads((tmp_path / "receipt.json").read_text())["outcome"] == "approved"
    assert await asyncio.to_thread(main, dry + ["--dry-run-deny", "dad"]) == 1
    assert json.loads((tmp_path / "receipt.json").read_text())["outcome"] == "rejected"


async def test_cli_verify_subcommand_roundtrip(dry, tmp_path, monkeypatch):
    monkeypatch.setenv("ASKAROUND_SIGNING_KEY", "k")
    await asyncio.to_thread(main, dry)
    assert await asyncio.to_thread(main, ["verify", str(tmp_path / "receipt.json")]) == 0


async def test_cli_policy_subcommand_writes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import askaround.extras as ex
    good = json.dumps({"rules": [{"match": "money > 200", "owners": ["mom", "dad"], "quorum": 2}]})
    monkeypatch.setattr(ex, "Kai", lambda: Kai(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": good}}]}))), api_key="k"))
    rc = await asyncio.to_thread(main, ["policy", "budget over $200 needs both parents",
                                        "--roster", os.path.join(ROOT, "roster.example.yaml"), "-o", "p.yaml"])
    assert rc == 0 and "money > 200" in (tmp_path / "p.yaml").read_text()
    # stdout mode and planner failure
    assert await asyncio.to_thread(main, ["policy", "x", "--roster", os.path.join(ROOT, "roster.example.yaml"), "-o", "-"]) == 0


async def test_cli_policy_without_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    assert await asyncio.to_thread(main, ["policy", "x", "-o", "-"]) == 2


# ---------------- Board live context ----------------
async def test_board_context_manager_renders_and_summarises():
    eng, _ = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"])], scripted({("dad", 1): [(0.2, "approved")]}))
    c = Console(record=True, width=120)
    async with Board(eng, c) as board:
        r = await eng.run()
    board.summary(r, "receipt.json")
    out = c.export_text()
    assert "APPROVED" in out and "d2" in out and "receipt.json" in out


# ---------------- Kai: 429 retry + re-plan parsing ----------------
async def test_kai_retries_429_then_succeeds():
    calls = []
    good = json.dumps({"decisions": [{"id": "d1", "question": "ok?", "owners": ["mom"], "quorum": 1}]})

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"retryAfterSeconds": 0})
        return httpx.Response(200, json={"choices": [{"message": {"content": good}}]})

    plan = await Kai(httpx.AsyncClient(transport=httpx.MockTransport(h)), api_key="k").plan("x", make_roster(), Policy())
    assert plan.decisions[0].id == "d1" and len(calls) == 2


async def test_kai_non_2xx_and_odd_shapes_are_planner_errors():
    for resp in (httpx.Response(500, json={}), httpx.Response(200, json={"nope": 1})):
        kai = Kai(httpx.AsyncClient(transport=httpx.MockTransport(lambda r, resp=resp: resp)), api_key="k")
        with pytest.raises(PlannerError):
            await kai.plan("x", make_roster(), Policy())


async def test_kai_replan_parses_and_rejects_non_pending_edits():
    state = [{"id": "d1", "status": "passed"}, {"id": "d2", "status": "pending"}]
    ok = json.dumps({"add": [], "edit": [{"id": "d2", "question": "New?"}], "drop": [], "reason": "r"})
    bad = json.dumps({"add": [], "edit": [{"id": "d1", "question": "hack"}], "drop": [], "reason": "r"})

    def kai_for(*contents):
        it = iter(contents)
        return Kai(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": next(it)}}]}))), api_key="k")

    rp = await kai_for("```json\n" + ok + "\n```").replan("t", state, make_roster(), Policy(), {"d1", "d2"})
    assert isinstance(rp, Replan) and rp.edit[0]["id"] == "d2"
    with pytest.raises(PlannerError):
        await kai_for(bad, bad).replan("t", state, make_roster(), Policy(), {"d1", "d2"})


async def test_ask_failure_reports_the_exception_class_not_a_typeerror():
    def boom(req):
        raise httpx.ConnectError("down")
    with pytest.raises(NotifyError, match="ConnectError"):
        await ask(notify_with(boom))


async def test_network_failure_on_ask_is_an_ask_error_in_the_receipt_not_an_internal_error():
    eng, _ = build([dec("d1", ["mom"])], scripted({}))

    def boom(req):
        raise httpx.ConnectError("down")
    eng.notify = notify_with(boom)
    r = await eng.run()
    ask_rec = r["asks"][0]
    assert ask_rec["status"] == "error" and "ConnectError" in ask_rec["note"] and r["outcome"] == "rejected"
    assert not any("internal error" in line for line in eng.log)


async def test_bad_roster_yaml_in_policy_subcommand_exits_2_not_traceback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("people: [unclosed")
    assert await asyncio.to_thread(main, ["policy", "x", "--roster", str(bad), "-o", "-"]) == 2
