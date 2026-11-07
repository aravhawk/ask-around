import json

import httpx
import pytest
import yaml
from rich.console import Console

from askaround.extras import compile_policy_cmd
from askaround.models import Policy, load_policy
from askaround.planner import Kai, PlannerError
from conftest import make_roster


def kai_returning(*contents):
    it = iter(contents)
    def h(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(it)}}]})
    return Kai(httpx.AsyncClient(transport=httpx.MockTransport(h)), api_key="k")


GOOD = json.dumps({"rules": [{"match": "money > 200", "owners": ["Mom", "dad"], "quorum": 2}],
                   "defaults": {"expires_s": 60, "veto_on_deny": True, "max_reasks": 1, "max_replans": 2}})


async def test_compile_policy_normalises_owners_and_roundtrips_through_yaml():
    pol = await kai_returning("```json\n" + GOOD + "\n```").compile_policy("budget over $200 needs both parents", make_roster())
    assert pol.rules[0].owners == ["mom", "dad"] and pol.rules[0].quorum == 2 and pol.default_expires_s == 60
    again = Policy.model_validate(yaml.safe_load(yaml.safe_dump(pol.as_yaml_dict())))
    assert again.rules[0].quorum == 2 and again.veto_on_deny is True


async def test_compile_policy_rejects_unknown_owner_and_impossible_quorum_after_retry():
    bad_owner = json.dumps({"rules": [{"match": "x", "owners": ["ghost"], "quorum": 1}]})
    bad_q = json.dumps({"rules": [{"match": "x", "owners": ["mom"], "quorum": 3}]})
    for bad in (bad_owner, bad_q):
        with pytest.raises(PlannerError):
            await kai_returning(bad, bad).compile_policy("x", make_roster())


async def test_compile_policy_retries_once_on_garbage():
    pol = await kai_returning("not json", GOOD).compile_policy("x", make_roster())
    assert len(pol.rules) == 1


async def test_cli_writes_policy_and_backs_up_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("BELWEAVE_API_KEY", "k")
    import askaround.extras as ex
    monkeypatch.setattr(ex, "Kai", lambda: kai_returning(GOOD))
    roster = tmp_path / "r.yaml"
    roster.write_text("people:\n  mom: {webhook: 'https://x/m'}\n  dad: {webhook: 'https://x/d'}\n")
    out = tmp_path / "policy.yaml"
    out.write_text("rules: []\n")
    rc = await compile_policy_cmd("budget over $200 needs both parents", str(roster), str(out), Console(quiet=True))
    assert rc == 0 and (tmp_path / "policy.yaml.bak").read_text() == "rules: []\n"
    p = load_policy(out)
    assert p.rules[0].owners == ["mom", "dad"]
