"""Live Activity on the requester's Notify service. Best effort: every error is swallowed.
Notify allows one fresh start per 60 s per device, so we start exactly once and PATCH after that.
`accepted` is not proof the activity is visible on the phone; delivery is not guaranteed to be instant."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("askaround.live")


class LiveActivity:
    def __init__(self, client: httpx.AsyncClient, webhook: str, key: str, title: str, min_interval: float = 1.5):
        self.client, self.base, self.key = client, webhook.rstrip("/") + "/live-activities", key
        self.title, self.min_interval = title[:80], min_interval
        self.activity_id: str | None = None
        self.enabled = True
        self._latest: tuple[str, float, str | None] | None = None
        self._sent: tuple[str, float, str | None] | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    async def _call(self, method: str, url: str, payload: dict | None = None) -> httpx.Response | None:
        try:
            return await self.client.request(method, url, json=payload, timeout=5)
        except Exception as e:  # never break the main flow
            log.warning("live activity %s failed: %s", method, type(e).__name__)
            return None

    async def start(self, status: str, progress: float = 0.0, subtitle: str | None = None) -> None:
        payload = {"title": self.title, "status": status[:200], "progress": _p(progress), "key": self.key}
        if subtitle:
            payload["subtitle"] = subtitle[:200]
        r = await self._call("POST", self.base, payload)
        try:
            ok = r is not None and r.status_code == 201 and r.json().get("accepted") is not False
            self.activity_id = str(r.json()["activityId"]) if ok else None
        except Exception:
            ok = False
        if not ok:
            self.enabled = False  # one failed start (e.g. the 60 s limit): stay quiet for the whole run
            return
        self._sent = (status, _p(progress), subtitle)
        self._worker = asyncio.create_task(self._loop())

    def push(self, status: str, progress: float, subtitle: str | None = None) -> None:
        """Sync + non-blocking: record the newest state; the worker PATCHes it (coalesced)."""
        if self.enabled and self.activity_id:
            self._latest = (status, _p(progress), subtitle)
            self._wake.set()

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._latest and self._latest != self._sent and self.activity_id:
                status, progress, subtitle = self._latest
                body = {"status": status[:200], "progress": progress}
                if subtitle:
                    body["subtitle"] = subtitle[:200]
                await self._call("PATCH", f"{self.base}/{self.activity_id}", body)
                self._sent = self._latest
            await asyncio.sleep(self.min_interval)

    async def end(self, status: str, progress: float = 1.0) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if not (self.enabled and self.activity_id):
            return
        # final status goes out in a PATCH first (so it is visible), then the activity is ended
        await self._call("PATCH", f"{self.base}/{self.activity_id}", {"status": status[:200], "progress": _p(progress)})
        await self._call("POST", f"{self.base}/{self.activity_id}/end")
        self.activity_id = None


def _p(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
