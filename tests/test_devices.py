from conftest import build, dec, make_roster, scripted


def ask_bodies(fake):
    return {u.rsplit("/", 1)[-1]: b for m, u, b, _ in fake.requests if m == "POST" and b and "response" in b}


async def test_hook_body_has_deviceIds_only_when_device_id_set():
    ro = make_roster()
    ro.people["mom"].device_id = "dev_MOM"
    eng, fake = build([dec("d1", ["mom", "dad"], 2)], scripted({}), roster=ro)
    r = await eng.run()
    bodies = ask_bodies(fake)
    assert bodies["mom"]["deviceIds"] == ["dev_MOM"]
    assert "deviceIds" not in bodies["dad"]
    assert r["outcome"] == "approved"


async def test_device_id_applies_to_escalations_and_reasks_too():
    ro = make_roster()
    ro.people["mom"].device_id, ro.people["dad"].device_id = "dev_MOM", "dev_DAD"
    eng, fake = build([dec("d1", ["mom"])], scripted({("mom", 1): [(0, "expired")]}), roster=ro)
    await eng.run()
    posts = [(u.rsplit("/", 1)[-1], b["deviceIds"]) for m, u, b, _ in fake.requests if m == "POST" and b and "response" in b]
    assert posts == [("mom", ["dev_MOM"]), ("dad", ["dev_DAD"]), ("mom", ["dev_MOM"])]


async def test_shared_user_answers_credited_to_the_person_asked_not_by_respondingUserId():
    # both phones are one Notify user: every answer carries the SAME respondingUserId, and no user_id is configured
    eng, fake = build([dec("d1", ["mom", "dad"], 2)], scripted({}), user_ids={"mom": "shared_user", "dad": "shared_user"})
    r = await eng.run()
    assert r["outcome"] == "approved"
    assert r["decisions"][0]["votes"] == {"mom": "yes", "dad": "yes"}
    by_person = {a["person"]: a["eventId"] for a in r["asks"]}
    assert by_person["mom"] != by_person["dad"]  # each answer is tied to its own event


def test_roster_loads_device_id_and_example_has_placeholder():
    import os
    from askaround.models import load_roster
    ro = load_roster(os.path.join(os.path.dirname(__file__), "..", "roster.example.yaml"))
    assert ro.people["dad"].device_id == "dev_REPLACE" and ro.people["mom"].device_id is None
