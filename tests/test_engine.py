import random

import pytest

from askaround.notify import CONSENT_STATUSES, KNOWN_STATUSES, Notify, NotifyError, is_consent
from conftest import build, dec, scripted


async def test_quorum_2_of_2_passes():
    eng, _ = build([dec("d1", ["mom", "dad"], 2)], scripted({}))
    r = await eng.run()
    assert r["outcome"] == "approved"
    assert r["decisions"][0]["votes"] == {"mom": "yes", "dad": "yes"}


async def test_one_deny_vetoes_and_cancels_others():
    eng, fake = build([dec("d1", ["mom", "dad"], 1)], scripted({("mom", 1): [(0, "denied")], ("dad", 1): [(60, "approved")]}))
    r = await eng.run()
    assert r["decisions"][0]["outcome"] == "failed"
    assert r["outcome"] == "rejected"
    dad_ask = [a for a in r["asks"] if a["person"] == "dad"][0]
    assert dad_ask["status"] == "canceled"
    assert any(m == "POST" and u.endswith("/cancel") for m, u, *_ in fake.requests)


async def test_expired_escalates_then_blocks_when_backup_says_no():
    eng, _ = build([dec("d1", ["mom"], 1)], scripted({("mom", 1): [(0, "expired")], ("dad", 1): [(0, "no")]}))
    r = await eng.run()
    assert r["decisions"][0]["outcome"] == "blocked"
    kinds = [(a["person"], a["kind"], a["status"]) for a in r["asks"]]
    assert ("dad", "escalation", "no") in kinds
    assert r["outcome"] == "blocked"


async def test_expired_escalation_yes_reasks_owner_once():
    eng, _ = build([dec("d1", ["mom"], 1)], scripted({("mom", 1): [(0, "expired")], ("dad", 1): [(0, "yes")], ("mom", 2): [(0, "approved")]}))
    r = await eng.run()
    assert r["outcome"] == "approved"
    assert [a["kind"] for a in r["asks"]] == ["primary", "escalation", "reask"]


async def test_out_of_reasks_blocks():
    eng, _ = build([dec("d1", ["mom"], 1)], scripted({("mom", 1): [(0, "expired")], ("mom", 2): [(0, "expired")]}))
    r = await eng.run()
    assert r["decisions"][0]["outcome"] == "blocked"
    assert len(r["asks"]) == 3  # mom, dad escalation, mom re-ask; then no more


async def test_dependency_waits_and_blocked_propagates():
    eng, fake = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"]), dec("d3", ["dad"], deps=["d2"])],
                      scripted({("mom", 1): [(0.2, "denied")]}))
    r = await eng.run()
    outs = {d["id"]: d["outcome"] for d in r["decisions"]}
    assert outs == {"d1": "failed", "d2": "blocked", "d3": "blocked"}
    assert not any(a["person"] == "dad" for a in r["asks"])  # dependents never asked anyone


async def test_dependency_ordering():
    eng, fake = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"])], scripted({("mom", 1): [(0.2, "approved")]}))
    r = await eng.run()
    assert r["outcome"] == "approved"
    posts = [u for m, u, *_ in fake.requests if m == "POST" and not u.endswith("/cancel")]
    assert posts == ["https://fake/mom", "https://fake/dad"]  # dad only asked after mom's d1 passed


async def test_independent_decisions_run_in_parallel():
    eng, fake = build([dec("d1", ["mom"]), dec("d2", ["dad"])], scripted({("mom", 1): [(0.3, "approved")], ("dad", 1): [(0.3, "approved")]}))
    r = await eng.run()
    assert r["outcome"] == "approved"


def test_is_consent_only_approved_and_yes():
    assert is_consent("approved") and is_consent("yes")
    for s in ["expired", "canceled", "denied", "no", "replied", "pending", "error", "unknown", "", None, 1, True,
              "Approved", "YES", " yes", "approved ", ["yes"], {"status": "yes"}]:
        assert not is_consent(s)
    assert CONSENT_STATUSES == {"approved", "yes"}


async def test_garbage_and_network_errors_are_never_consent():
    import httpx
    def boom(req):
        if req.method == "POST":
            return httpx.Response(200, json={"ok": True, "eventId": "e1"})
        raise httpx.ConnectError("down")
    n = Notify(httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(NotifyError):
        await n.read("https://x/y", "e1")
    for body in [{}, {"event": {}}, {"event": {"response": {}}}, {"event": {"response": {"status": 5}}},
                 {"event": {"response": {"status": "weird"}}}]:
        n = Notify(httpx.AsyncClient(transport=httpx.MockTransport(lambda r, b=body: httpx.Response(200, json=b))))
        st = await n.read("https://x/y", "e1")
        assert not is_consent(st.status)


async def test_network_failure_during_poll_is_no_not_yes():
    import httpx
    eng, fake = build([dec("d1", ["mom"])], scripted({}))
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"ok": True, "eventId": "e1"})
        raise httpx.ConnectError("down")
    eng.notify = Notify(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await eng.run()
    assert r["outcome"] != "approved"
    assert r["decisions"][0]["outcome"] == "failed"


@pytest.mark.parametrize("seed", range(40))
async def test_no_false_approvals_over_random_status_sequences(seed):
    rng = random.Random(seed)
    pool = sorted(KNOWN_STATUSES) + ["garbage", "APPROVED", ""]
    table = {}
    for p in ("mom", "dad"):
        for n in range(1, 4):
            steps, t = [], 0.0
            for _ in range(rng.randint(1, 4)):
                steps.append((t, rng.choice(pool)))
                t += rng.random() * 0.03
            table[(p, n)] = steps
    eng, _ = build([dec("d1", ["mom", "dad"], rng.choice([1, 2])), dec("d2", ["mom"], deps=["d1"])], scripted(table))
    r = await eng.run()
    # Invariant: any decision that passed is backed by >= quorum asks whose FINAL recorded status is consent.
    for d in r["decisions"]:
        if d["outcome"] == "passed":
            yes_people = {a["person"] for a in r["asks"] if a["decision"] == d["id"] and a["kind"] in ("primary", "reask")
                          and is_consent(a["status"])}
            assert len(yes_people) >= d["quorum"], (seed, d)
        if r["outcome"] == "approved":
            assert d["outcome"] == "passed"
