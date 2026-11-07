import json
import copy

from rich.console import Console

from askaround.signing import canonical, sign_receipt, verify_cmd, verify_receipt
from askaround.runner import run_task
import os

R = {"task": "t", "outcome": "approved", "asks": [{"person": "mom", "status": "approved"}], "decisions": []}


def test_sign_and_verify_roundtrip_and_tamper_detection():
    s = sign_receipt(R, "k1")
    assert s["signature"]["alg"] == "HMAC-SHA256" and "signature" not in R
    assert verify_receipt(s, "k1")[0]
    for tamper in (lambda r: r.update(outcome="rejected"),
                   lambda r: r["asks"][0].update(status="denied"),
                   lambda r: r.update(extra=1),
                   lambda r: r["signature"].update(value="0" * 64)):
        t = copy.deepcopy(s)
        tamper(t)
        assert not verify_receipt(t, "k1")[0]
    assert not verify_receipt(s, "other-key")[0]


def test_unsigned_or_keyless_never_verifies_and_key_never_in_output():
    assert not verify_receipt(R, "k")[0]
    assert not verify_receipt(sign_receipt(R, "k"), "")[0]
    assert sign_receipt(R, "") is R
    assert "supersecretkey" not in json.dumps(sign_receipt(R, "supersecretkey"))


def test_canonical_ignores_key_order_and_signature():
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1, "signature": {"value": "zzz"}}
    assert canonical(a) == canonical(b)


def test_verify_cmd_exit_codes(tmp_path, monkeypatch):
    f = tmp_path / "r.json"
    c = Console(quiet=True)
    monkeypatch.setenv("ASKAROUND_SIGNING_KEY", "k")
    f.write_text(json.dumps(sign_receipt(R)))
    assert verify_cmd([str(f)], c) == 0
    d = json.loads(f.read_text()); d["outcome"] = "rejected"; f.write_text(json.dumps(d))
    assert verify_cmd([str(f)], c) == 1
    monkeypatch.delenv("ASKAROUND_SIGNING_KEY")
    assert verify_cmd([str(f)], c) == 2
    assert verify_cmd([str(tmp_path / "nope.json")], c) == 2


async def test_run_task_signs_receipt_file_when_key_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKAROUND_SIGNING_KEY", "demo-key")
    monkeypatch.delenv("BELWEAVE_API_KEY", raising=False)
    root = os.path.join(os.path.dirname(__file__), "..")
    r = await run_task("Plan a trip", os.path.join(root, "roster.example.yaml"), os.path.join(root, "policy.yaml"),
                       dry_run=True, receipt_path=str(tmp_path / "r.json"), show_board=False, console=Console(quiet=True), speed=0.05)
    on_disk = json.loads((tmp_path / "r.json").read_text())
    assert verify_receipt(on_disk)[0] and verify_receipt(r)[0]
    assert "demo-key" not in (tmp_path / "r.json").read_text()
