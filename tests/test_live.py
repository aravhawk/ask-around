import asyncio

import httpx

from askaround.liveactivity import LiveActivity
from conftest import build, dec, scripted


def live_calls(fake):
    return [(m, u.split("/live-activities")[1] if "/live-activities" in u else None, b)
            for m, u, b, _ in fake.requests if "/live-activities" in u]


async def test_live_activity_start_once_update_end():
    eng, fake = build([dec("d1", ["mom"]), dec("d2", ["dad"], deps=["d1"])],
                      scripted({("mom", 1): [(0.2, "approved")], ("dad", 1): [(0.2, "approved")]}))
    eng.live = LiveActivity(eng.notify.client, "https://fake/me", eng.run_id, "Dallas trip", min_interval=0.05)
    r = await eng.run()
    calls = live_calls(fake)
    assert r["outcome"] == "approved"
    posts = [c for c in calls if c[0] == "POST" and c[1] == ""]
    assert len(posts) == 1 and posts[0][2]["key"] == eng.run_id and posts[0][2]["title"] == "Dallas trip"
    assert posts[0][2]["status"].startswith("0 of 2 decided") and 0 <= posts[0][2]["progress"] <= 1
    patches = [c for c in calls if c[0] == "PATCH"]
    assert any("waiting on Mom" in p[2]["status"] for p in patches)
    assert patches[-1][2]["status"] == "Approved by Mom + Dad · receipt saved" and patches[-1][2]["progress"] == 1.0
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/end")
    assert sum(1 for c in calls if c[1] and c[1].endswith("/end")) == 1


async def test_live_activity_errors_never_break_the_run():
    for status in (400, 429, 500):
        eng, fake = build([dec("d1", ["mom"])], scripted({}))
        fake.live_status = status
        eng.live = LiveActivity(eng.notify.client, "https://fake/me", eng.run_id, "t", min_interval=0.01)
        r = await eng.run()
        assert r["outcome"] == "approved"
        assert len([c for c in live_calls(fake) if c[0] == "POST"]) == 1  # no retry-start after a failed start


async def test_live_activity_network_error_and_exceptions_swallowed():
    def boom(req):
        raise httpx.ConnectError("down")
    la = LiveActivity(httpx.AsyncClient(transport=httpx.MockTransport(boom)), "https://x/y", "k", "t")
    await la.start("s", 0.0)
    la.push("s2", 0.5)
    await la.end("done")
    assert la.enabled is False

    eng, _ = build([dec("d1", ["mom"])], scripted({}))
    class Exploding:
        async def start(self, *a): raise RuntimeError("x")
        def push(self, *a): raise RuntimeError("x")
        async def end(self, *a): raise RuntimeError("x")
    eng.live = Exploding()
    assert (await eng.run())["outcome"] == "approved"


async def test_live_final_text_for_rejection_never_says_approved():
    eng, fake = build([dec("d1", ["mom"])], scripted({("mom", 1): [(0, "denied")]}))
    eng.live = LiveActivity(eng.notify.client, "https://fake/me", eng.run_id, "t", min_interval=0.01)
    await eng.run()
    last_patch = [c for c in live_calls(fake) if c[0] == "PATCH"][-1]
    assert last_patch[2]["status"].startswith("Rejected") and "Approved" not in last_patch[2]["status"]


async def test_no_requester_means_no_live_activity_calls():
    eng, fake = build([dec("d1", ["mom"])], scripted({}))
    await eng.run()
    assert live_calls(fake) == []
