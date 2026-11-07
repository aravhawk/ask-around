"""Fake Notify transport for --dry-run and tests. Implements only the documented endpoints."""
from __future__ import annotations

import itertools
import json
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

Script = Callable[[str, int, dict], list[tuple[float, str]]]
# Script(person, nth_ask_for_person, payload) -> [(seconds_after_ask, status), ...] steps; last step is final.


def demo_script(silent: set[str] | None = None, deny: set[str] | None = None, speed: float = 1.0) -> Script:
    silent, deny = silent or set(), deny or set()
    order: dict[str, int] = {}

    def script(person: str, nth: int, payload: dict):
        idx = order.setdefault(person, len(order))
        yes_type = payload["response"]["type"]
        ok = "approved" if yes_type == "approval" else "yes"
        no = "denied" if yes_type == "approval" else "no"
        if person in deny:
            return [(2 * speed, no)]
        if person in silent and nth == 1:
            return [(5 * speed, "expired")]
        return [((1.5 + idx) * speed, ok)]

    return script


class FakeNotify:
    """Behaves like Notify webhook services, one per person (the person is the URL path)."""

    def __init__(self, script: Script, clock: Callable[[], float] = time.monotonic, user_ids: dict | None = None):
        self.script, self.clock, self._ids = script, clock, itertools.count(1)
        self.events: dict[str, dict] = {}
        self.per_person: dict[str, int] = {}
        self.user_ids = user_ids or {}
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = urlparse(str(req.url)).path.strip("/").split("/")
        person = path[0]
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, str(req.url), body, dict(req.headers)))
        if req.method == "POST" and len(path) == 1:
            n = self.per_person[person] = self.per_person.get(person, 0) + 1
            eid = f"evt_{next(self._ids)}"
            steps = self.script(person, n, body)
            self.events[eid] = {"person": person, "t0": self.clock(), "steps": steps, "canceled": False}
            return httpx.Response(200, json={"ok": True, "eventId": eid, "delivered": 1,
                                             "response": {"status": "pending", "expiresAt": "2099-01-01T00:00:00Z"}})
        ev = self.events.get(path[-1] if path[-2:-1] != ["events"] else path[-1]) if len(path) >= 3 else None
        if req.method == "GET" and len(path) == 3 and path[1] == "events":
            ev = self.events[path[2]]
            status, extra = self._status(ev)
            resp = {"status": status, **extra}
            return httpx.Response(200, json={"event": {"id": path[2], "response": resp}})
        if req.method == "POST" and len(path) == 4 and path[3] == "cancel":
            ev = self.events[path[2]]
            status, _ = self._status(ev)
            if status != "pending":
                return httpx.Response(404, json={"ok": False})
            ev["canceled"] = True
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    def _status(self, ev: dict):
        if ev["canceled"]:
            return "canceled", {}
        age = self.clock() - ev["t0"]
        status = "pending"
        for at, st in ev["steps"]:
            if age >= at:
                status = st
        extra = {}
        if status not in ("pending", "expired", "canceled"):
            extra = {"respondedAt": "2026-01-01T00:00:00Z", "action": status}
            uid = self.user_ids.get(ev["person"])
            if uid:
                extra["respondingUserId"] = uid
        return status, extra
