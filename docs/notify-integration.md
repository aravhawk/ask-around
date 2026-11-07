# Notify integration

Ask Around talks to Notify only through each person's webhook service URL (`https://notify.belweave.ai/hooks/<token>`). The token is a secret. It lives only in `roster.yaml`. The `httpx` loggers are pinned to WARNING so request URLs never reach logs.

## Endpoints

### Ask: `POST {webhook}`

Headers: `Idempotency-Key: <runId>-<correlationId>-<n>` (at most 200 chars, unique per ask).

```json
{ "title": "Ask Around · parent · budget",
  "body": "Approve the $600 trip budget?",
  "response": { "type": "approval", "expiresInSeconds": 30, "correlationId": "d1-mom" },
  "deviceIds": ["<device id>"] }
```

- `title` is cut to 80 chars and `body` to 2000. `correlationId` is `<decision>-<person>`, with a suffix for escalations and re-asks, cut to 100.
- `type` is `approval`, `yes_no` or `text`. `expiresInSeconds` is clamped to 30–86400.
- `deviceIds` appears **only** when the person has a `device_id` in the roster, and is omitted otherwise.
- Success is `{ok, eventId, delivered, response: {status: "pending", expiresAt}}`. Anything else (non-2xx, `ok` false, missing `eventId`, network error) means no ask was made, which counts as NO.

### Read: `GET {webhook}/events/{eventId}`

Polled every 2 s. Reads `event.response.status` (`pending`, `approved`, `denied`, `yes`, `no`, `replied`, `expired`, `canceled`), plus `respondedAt`, `text`, and `respondingUserId` when present. Reading an overdue pending ask makes Notify settle it as `expired`. If an ask is still pending 45 s after its deadline, Ask Around treats it as expired locally. Four consecutive read errors end the ask as `error`.

### Cancel: `POST {webhook}/events/{eventId}/cancel`

Sent for every pending ask when its decision settles. A `404` (already settled) or any error is fine and ignored. If a decision settles while an ask is still being delivered, Ask Around waits for the delivery, records the eventId, and cancels it.

### Live Activity (requester's service, best effort)

- `POST {webhook}/live-activities` with `{"title", "status", "subtitle"?, "progress": 0-1, "key": "<runId>"}`. It expects `201` with an `activityId`. A non-201, a missing `activityId`, or `accepted` of `0`/`false` means "not started", and Live Activity stays off for the run. Started **once** per run, because Notify allows one fresh start per 60 s per device.
- `PATCH {webhook}/live-activities/{activityId}` with `{"status", "subtitle"?, "progress"}`. Updates are coalesced (at least 1.5 s apart) and sent only when the text changes, e.g. `2 of 3 decided · waiting on Mom`.
- At the end, a final `PATCH` (e.g. `Approved by Dad + Mom · receipt saved`), then `POST …/live-activities/{activityId}/end`.

Every Live Activity error is swallowed. It can never break or change the approval flow, and `accepted` is not proof it is visible on the phone.

### Rate limits and errors

`429`: wait `retryAfterSeconds` from the body (default 60 s), up to 3 retries. `400`: the `issues` array is logged. Kai-4 calls (`api.infer.belweave.ai`) are limited to 2 concurrent requests.

## Fail-closed consent

One function decides consent (`notify.is_consent`): the status must be exactly `approved` or `yes` (case-sensitive). All of these are **NO**: `denied`, `no`, `expired`, `canceled`, `replied` on an approval or yes/no, `pending` at timeout, `error`, any unknown string, any non-string, and any exception inside the engine.

A `replied` with non-empty text satisfies only a `text` decision, as information. It never lets an approval or yes/no pass. The test suite includes a randomized test that pushes random and garbage status sequences through the engine and asserts no decision passes without enough consent.

## One service per person

Notify settles a prompt on the first valid answer from anyone who can see it. For strict routing, each person creates a service in their own Notify account and shares only its webhook URL. If several people must share one account (for example two demo phones), use `device_id` to target one device per person. Answers are then credited by event id, not by `respondingUserId`.
