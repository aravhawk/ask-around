# How it works

Source map: `planner.py` (Kai-4), `models.py` (validation), `engine.py` (quorum engine), `notify.py` (consent rule), `board.py`, `liveactivity.py`, `signing.py`.

## 1. Planner

`Kai.plan(task, roster, policy)` calls `POST https://api.infer.belweave.ai/v1/chat/completions` with model `Belweave/Kai-4`, `max_tokens` 4000 (the model uses hidden reasoning tokens, so small limits return empty content), and at most 2 requests in flight. It sends the task, the roster's keys, names, roles and backups, and the policy rules. **Webhook URLs are never sent.**

Kai-4 must return JSON only:

```json
{"decisions":[{"id":"d1","question":"...","type":"approval|yes_no|text","owners":["mom","dad"],"quorum":2,"depends_on":[],"expires_s":30}]}
```

Code fences are stripped and the result is validated with pydantic. On a parse or validation failure it retries once with the error, then raises `PlannerError`. Validation (`validate_plan`) normalises owners to roster keys (key or display name) and rejects:

- a duplicate or empty decision id,
- an owner not in the roster, or a decision with no owners,
- a `quorum` outside `1..len(owners)`,
- a dependency on an unknown id (or itself), and dependency cycles.

Then policy is applied: keyword rules add owners and raise quorum, `require_all_owners` sets quorum to the owner count (after rules, so rule-added owners count), and `expires_s` is clamped by `max_expires_s` or overridden by `--expires`. See [policy.md](policy.md).

## 2. Decision graph

Decisions form a DAG through `depends_on`. A decision is **ready** when every dependency has `passed`. All ready decisions start in parallel. A decision whose dependency `failed`, was `blocked` or was `dropped` becomes `blocked` without asking anyone.

Decision states: `waiting` → `running` → `passed` | `failed` | `blocked`, plus `dropped` (re-plan only). Decision types: `approval` and `yes_no` need consent; `text` collects information.

## 3. Engine: quorum and veto

Each owner runs in their own task. The owner's vote ends as one of `yes`, `no`, `blocked` (silent), or `cancelled`.

| Event | Result |
|---|---|
| `approved`/`yes` (approval, yes_no) | vote `yes` |
| non-empty `replied` on a `text` decision | vote `yes` (information, not consent) |
| `denied`, `no`, `canceled`, empty reply, ask error, unknown status | vote `no` |
| `expired` | silence escalation, below |

A decision resolves as soon as one of these holds, checked after each batch of finished owners:

1. **Veto:** any `no` (with `veto_on_deny: true`, the default) → `failed`, immediately. This is checked before counting yes votes.
2. **Quorum:** `yes >= quorum` → `passed`.
3. **Unreachable:** `yes + still_pending < quorum` → `failed` if any owner said no, otherwise `blocked`.

When it resolves, the remaining owner tasks are cancelled and each cancelled ask is sent `POST …/cancel`. With `veto_on_deny: false`, a `no` just doesn't count, and the decision fails only when quorum becomes unreachable.

Example: owners `[mom, dad]`, quorum 2. Dad yes → 1/2, pending. Mom no → `failed`. Mom silent and unrecoverable → `blocked`. Both yes → `passed`.

## 4. Silence escalation

When an owner's ask comes back `expired` (or stays pending 45 s past its deadline):

1. If the owner has no `backup`, or `max_reasks` (default 1) is used up → vote `blocked`.
2. Otherwise the backup gets a `yes_no`: `"<owner> hasn't answered: <question>. Ask again?"`.
3. Only a server-confirmed `yes` from the backup re-asks the owner. Anything else → `blocked`.

The backup's yes authorises a re-ask only. It is never a vote on the decision, and it doesn't appear in `votes`. Silence never turns into approval. Polling treats 4 consecutive read errors as NO.

## 5. Re-planning

After a decision settles, Kai-4 receives the current state (statuses, answers truncated to 300 characters) and may return `{add, edit, drop, reason}`. At most 2 re-plans run per task (`max_replans` is clamped to 2). They run in the background, so ready decisions are never delayed. No re-plan runs once any decision has failed or been blocked.

**A re-plan can only tighten governance:**

- Only decisions still `waiting` can be edited or dropped. Edits aimed at decisions that already started or settled are skipped as stale and recorded.
- An edit may not remove owners, lower the quorum, remove a dependency, or turn `approval`/`yes_no` into `text`.
- Only `text` decisions can be dropped.
- The whole resulting graph is re-validated (owners, quorum, dependencies, cycles, policy rules). If anything fails, the **entire** re-plan is rejected and the plan is unchanged.
- The receipt records only what was applied, plus the proposed ids and a `summary`.

Text replies from people reach Kai-4 as data, so tighten-only validation is what keeps an injected instruction from weakening a decision.

## 6. Outputs

- Rich live board: decision, owners, votes (`yes/quorum`), status, seconds left, re-plans, a log. Board errors never affect the outcome.
- Live Activity on the requester's service (best effort; [notify-integration.md](notify-integration.md)).
- `receipt.json` ([receipts.md](receipts.md)). Overall outcome: `approved` (every non-dropped decision passed), `rejected` (any failed), else `blocked`.

## 7. Wrong-person guard

If a decision's person has `user_id` set and an answer arrives with a different `respondingUserId`, the answer is ignored and the question is re-asked (up to 2 times, then NO). With no `user_id` set, the check is skipped and the answer is credited to the person the ask was sent to, found by event id. Use this when people share one Notify account.
