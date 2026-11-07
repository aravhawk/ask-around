import json
import logging
import os
import sys

import pytest


async def test_ask_around_impl_dry_run_returns_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKAROUND_DRY_RUN", "1")
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    from askaround.mcp_server import ask_around_impl
    r = await ask_around_impl("Plan a trip", os.path.join(os.path.dirname(__file__), "..", "roster.example.yaml"),
                              os.path.join(os.path.dirname(__file__), "..", "policy.yaml"))
    assert r["outcome"] == "approved" and r["asks"] and r["plan"]["decisions"]
    assert list(tmp_path.glob("receipt-*.json"))


async def test_ask_around_impl_errors_are_not_approvals(tmp_path):
    from askaround.mcp_server import ask_around_impl
    r = await ask_around_impl("x", str(tmp_path / "missing.yaml"))
    assert r["outcome"] == "error"
    bad = tmp_path / "bad.yaml"
    bad.write_text("people: nonsense")
    assert (await ask_around_impl("x", str(bad)))["outcome"] == "error"


async def test_webhook_urls_never_appear_in_logs_even_at_info(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    import askaround  # noqa: F401  (pins httpx loggers)
    import httpx
    from askaround.fake import FakeNotify, demo_script
    from askaround.engine import Engine
    from askaround.notify import Notify
    from askaround.models import Plan, Policy
    from conftest import dec, make_roster
    ro = make_roster()
    for p in ro.people.values():
        p.webhook_url = f"https://fake.notify/{p.key}"
    fake = FakeNotify(demo_script(speed=0.01))
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    eng = Engine("t", Plan(decisions=[dec("d1", ["mom"])]), ro, Policy(),
                 Notify(httpx.AsyncClient(transport=fake.transport())), poll_interval=0.01)
    await eng.run()
    assert "fake.notify" not in caplog.text
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_server_registers_the_tool():
    from askaround.mcp_server import build_server
    assert build_server() is not None


async def test_mcp_stdio_roundtrip_keeps_stdout_clean(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = os.path.join(os.path.dirname(__file__), "..")
    env = dict(os.environ, ASKAROUND_DRY_RUN="1")
    env.pop("BELWEAVE_API_KEY", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "askaround.mcp_server"], env=env, cwd=str(tmp_path))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            assert "ask_around" in [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool("ask_around", {"task": "Plan a trip", "roster_path": os.path.abspath(os.path.join(root, "roster.example.yaml")),
                                                   "policy_path": os.path.abspath(os.path.join(root, "policy.yaml"))})
            assert json.loads(res.content[0].text)["outcome"] == "approved"
