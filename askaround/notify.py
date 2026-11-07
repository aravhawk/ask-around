"""Notify client + the consent rule. Fail closed everywhere."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("askaround.notify")

# The ONLY statuses that count as consent. Everything else (including unknown) is NO.
CONSENT_STATUSES = frozenset({"approved", "yes"})
KNOWN_STATUSES = frozenset({"pending", "approved", "denied", "yes", "no", "replied", "expired", "canceled"})


def is_consent(status: Any) -> bool:
    """True only for a server-confirmed 'approved' or 'yes' (exact, case-sensitive string)."""
    return isinstance(status, str) and status in CONSENT_STATUSES


class NotifyError(Exception):
    pass


@dataclass
class EventState:
    status: str
    action: str | None = None
    text: str | None = None
    responded_at: str | None = None
    responding_user_id: str | None = None
    raw_ok: bool = True


@dataclass
class Notify:
    client: httpx.AsyncClient
    max_429_waits: int = 3
    default_retry_after: float = 60.0
    sleep: Any = field(default=asyncio.sleep)

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(self.max_429_waits + 1):
            r = await self.client.request(method, url, timeout=20, **kw)
            if r.status_code == 429 and attempt < self.max_429_waits:
                wait = self.default_retry_after
                try:
                    wait = float(r.json().get("retryAfterSeconds", wait))
                except Exception:
                    pass
                log.warning("notify 429, waiting %ss", wait)
                await self.sleep(wait)
                continue
            if r.status_code == 400:
                try:
                    log.error("notify 400 issues: %s", r.json().get("issues"))
                except Exception:
                    log.error("notify 400 (unparseable body)")
            return r
        return r  # pragma: no cover

    async def ask(self, webhook: str, *, title: str, body: str, type: str, expires_s: int,
                  correlation_id: str, idempotency_key: str) -> tuple[str, str | None]:
        payload = {
            "title": title[:80],
            "body": body[:2000],
            "response": {"type": type, "expiresInSeconds": max(30, min(86400, int(expires_s))),
                         "correlationId": correlation_id[:100]},
        }
        try:
            r = await self._request("POST", webhook, json=payload, headers={"Idempotency-Key": idempotency_key[:200]})
            data = r.json()
        except Exception as e:  # network / decode errors => no ask => NO
            raise NotifyError(f"ask failed: {type(e).__name__}") from e
        if r.status_code >= 300 or not data.get("ok") or not data.get("eventId"):
            raise NotifyError(f"ask rejected: http {r.status_code}")
        resp = data.get("response") or {}
        return str(data["eventId"]), resp.get("expiresAt")

    async def read(self, webhook: str, event_id: str) -> EventState:
        try:
            r = await self._request("GET", f"{webhook.rstrip('/')}/events/{event_id}")
            if r.status_code >= 300:
                raise NotifyError(f"read http {r.status_code}")
            data = r.json()
        except NotifyError:
            raise
        except Exception as e:
            raise NotifyError(f"read failed: {type(e).__name__}") from e
        ev = data.get("event", data) if isinstance(data, dict) else {}
        resp = ev.get("response") if isinstance(ev, dict) else None
        if not isinstance(resp, dict) or not isinstance(resp.get("status"), str):
            return EventState(status="unknown", raw_ok=False)
        return EventState(
            status=resp["status"],
            action=resp.get("action"),
            text=resp.get("text"),
            responded_at=resp.get("respondedAt"),
            responding_user_id=resp.get("respondingUserId"),
        )

    async def cancel(self, webhook: str, event_id: str) -> None:
        try:
            await self._request("POST", f"{webhook.rstrip('/')}/events/{event_id}/cancel")
        except Exception:  # 404 / already settled / network: all fine
            pass
