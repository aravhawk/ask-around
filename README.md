# Ask Around

**Governance for agents that ask people.** One tool call, `ask_around(task, roster, policy)`, and an agent can get real humans to decide, with the right person, enough people, and a safe answer to silence.

Notify's own docs describe how a question settles:

> Family: "Any accepted family member … may respond, and the first valid server-confirmed answer settles the prompt."

> Workplace: "Group questions settle on the first valid answer from a current group member."

> Devices: `deviceIds` only reach your own devices: "A foreign or unknown selection returns `400 Invalid device selection`."

That is exactly right for "is the stove off?". It is not enough when an agent asks about money, access, or plans.

**Ask Around adds who may answer, how many must agree, and what silence means.**

## What it does

```
task ─▶ Planner (Belweave Kai-4) ─▶ decision graph ─▶ dispatcher ─▶ Notify webhooks (one per person)
             ▲                            │                │
             └──── re-planner ◀──── state ◀──── poller (every 2 s)
                                          │
                      rich board · Live Activity · signed receipt.json
```

1. **Plan.** Kai-4 splits the task into decisions `{id, question, type, owners[], quorum, depends_on[], expires_s}`. The output is validated with pydantic (owners must be in the roster, `quorum <= len(owners)`, no unknown dependencies or cycles) and retried once on a parse failure. The planner sees roster keys, names and roles. It never sees webhook URLs.
2. **Ask only the owner.** Each ready decision goes through Notify to each owner's own webhook, with an `Idempotency-Key` per ask.
3. **Quorum + veto.** A decision passes when `quorum` owners give a server-confirmed approve or yes. Any deny or no fails it at once, and the other pending asks are canceled (`POST /events/:id/cancel`).
4. **Silence is NO.** When an ask expires, the owner's backup gets a yes/no: *"{owner} hasn't answered: {question}. Ask again?"*. Yes re-asks the owner (up to `max_reasks`, default 1). No, expired, or out of re-asks marks the decision `blocked`, and everything that depends on it is blocked too.
5. **Dependencies.** A decision is asked only after everything in `depends_on` has passed. Independent decisions run in parallel.
6. **Re-plan.** After a decision settles, Kai-4 sees the state and may add, edit or drop **pending** decisions (max 2 re-plans). It runs in the background, so it never delays real asks. A re-plan can only *tighten*: an edit may not remove owners, lower the quorum, remove a dependency, or turn an approval into free text. Only informational `text` decisions may be dropped. The whole resulting graph is re-validated (owners, quorum, dependencies, cycles, policy rules) before it is applied. A re-plan that fails any check is rejected whole and the plan stays as it was. Edits aimed at decisions that already started are skipped as stale. The receipt records only what was actually applied. No re-plan runs once any decision has failed or been blocked.
7. **Wrong-person guard.** If a response carries a `respondingUserId` that isn't the expected person's `user_id` (set it in the roster for a shared Family service), the answer is ignored and the question is re-asked. Per-person services make this impossible by design, which is the recommended setup.
8. **Live board, Live Activity, receipt.** A rich terminal board shows decision, owners, votes (like `1/2`), status and seconds left. The requester's phone gets one Live Activity. `receipt.json` records the task, plan, re-plans, every ask (person, exact prompt, eventId, status, respondedAt, text) and the outcome per decision. It is HMAC-signed when you set a key.

## The consent rule

> **Only a server-confirmed `approved` or `yes` counts as consent.** `expired`, `canceled`, `denied`, `no`, `replied` (on an approval or yes/no question), network errors, malformed responses and any status we don't recognise all count as **NO**.

This is Notify's own rule, extended to groups. It lives in one function (`askaround.notify.is_consent`: exact, case-sensitive match on `approved` or `yes`) and is covered by unit tests, including a randomized test that feeds random status sequences, garbage statuses and network failures through the engine and asserts that no decision ever passes without enough consent behind it.

Details worth knowing:

- Internal errors are NO. Any exception inside an owner's flow becomes a NO vote.
- A `text` decision (information gathering, like "hotel preference?") is satisfied by a non-empty `replied`. That is information, not consent. It never lets an approval or yes/no decision pass. The planner is told to use `approval` for anything that needs consent.
- Once `quorum` is reached, the remaining pending asks are canceled. A later deny in a 1-of-2 decision is therefore not seen. Use `quorum = len(owners)` (or `require_all_owners: true`) when everyone must agree.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp,dev]"

# 1. One Notify webhook service PER PERSON, created in THAT person's own account
#    (Dashboard → Services). Each person shares only their webhook URL.
cp roster.example.yaml roster.yaml        # roster.yaml is git-ignored: webhook URLs are secrets
$EDITOR roster.yaml policy.yaml

# 2. Planner key (read from the environment, never logged)
export BELWEAVE_API_KEY=...

# 3. Run it
python -m askaround "Plan a \$600 weekend trip to Dallas for the Karan Aujla concert" \
  --roster roster.yaml --policy policy.yaml
```

No phones handy? `--dry-run` fakes Notify (in-process, no network for Notify). One person is silent on their first ask so you can watch the escalation. Without `BELWEAVE_API_KEY` it uses a built-in offline plan. With the key it uses real Kai-4 planning and re-planning.

```bash
python -m askaround "Plan a \$600 weekend trip to Dallas" --roster roster.example.yaml --policy policy.yaml --dry-run
python -m askaround "..." --roster roster.example.yaml --dry-run --dry-run-deny dad     # watch a veto
python -m askaround "..." --roster roster.example.yaml --dry-run --dry-run-silent mom   # choose who goes silent
```

Exit code: `0` approved, `1` rejected or blocked, `2` any error (bad config, unreachable planner, interrupted), so a wrapper never mistakes an error for a rejection.

### `roster.yaml` (PRD §7)

```yaml
requester: arav
people:
  arav: { role: "requester", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: null }
  dad:  { role: "parent · budget, hotel", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
  mom:  { role: "parent · budget", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
  # optional per person: name (defaults to the key, title-cased), user_id (wrong-person guard),
  # device_id (adds "deviceIds": [id] to that person's asks, for people sharing one Notify account)
```

`webhook_url:` is accepted as an alias for `webhook:`. The requester's webhook also hosts the Live Activity.

### `policy.yaml` (PRD §7)

```yaml
rules:
  - match: "money > 200"      # Kai-4 applies these while planning
    owners: [mom, dad]
    quorum: 2
  - match: "hotel"            # plain keywords are ALSO enforced deterministically after planning
    owners: [dad]
    quorum: 1
defaults: { expires_s: 30, max_expires_s: 30, veto_on_deny: true, max_reasks: 1, max_replans: 2 }
```

- `max_expires_s` clamps every decision's `expires_s` after planning *and* re-planning, because the planner may copy any number. Without it, silence takes as long as the planner said. `--expires N` on the CLI overrides every decision's `expires_s` (30–86400).
- Expression rules (`money > 200`) are applied by the planner. Plain-keyword rules (`hotel`) are also enforced in code: any decision mentioning the keyword gets the rule's owners and at least its quorum. This also applies to decisions added by a re-plan. Rules can only tighten, never loosen.
- `require_all_owners: true` (extra) forces quorum = every owner of every decision.
- The older flat keys (`max_reasks`, `default_expires_s`) and plain-string rules still load.
- `max_replans` is clamped to 2.
- **Policies fail closed.** `--policy` defaults to `./policy.yaml` when present. A policy path that doesn't exist is an error (a typo can't silently disable your rules), as is a rule whose `owners` aren't in the roster. With no policy file at all the run proceeds with built-in defaults and a visible warning.
- `require_all_owners` is applied after rules add owners, so it really means everyone.

### Plain-English policy

```bash
python -m askaround policy "budget over \$200 needs both parents" --roster roster.yaml   # → policy.yaml
```

Kai-4 compiles the sentence into the format above. Owners are checked against your roster, and `quorum <= len(owners)` is enforced. An existing `policy.yaml` is saved as `policy.yaml.bak`. Use `-o -` to print instead of writing.

### Signed receipts

```bash
export ASKAROUND_SIGNING_KEY="a long random string"     # never printed, never stored
python -m askaround "..." --roster roster.yaml           # receipt.json now carries an HMAC-SHA256 signature
python -m askaround verify receipt.json                  # exit 0 valid · 1 tampered/unsigned · 2 no key/unreadable
```

The signature covers the canonical JSON of the whole receipt except the signature block. Change any ask, status, or outcome and `verify` fails. This is tamper-evidence for someone who holds the key, not public-key non-repudiation.

## MCP: use it from Claude Code (or Codex)

The server speaks MCP over stdio and exposes one tool: `ask_around(task, roster_path, policy_path)`. It returns the receipt as JSON. Tell the agent that only `outcome == "approved"` means go. The receipt is also written to `receipt-<runId>.json` in the server's working directory.

Claude Code, one line (use absolute paths):

```bash
claude mcp add --env BELWEAVE_API_KEY="$BELWEAVE_API_KEY" ask-around -- /abs/path/to/ask-around/.venv/bin/python -m askaround.mcp_server
```

Or a project `.mcp.json`:

```json
{
  "mcpServers": {
    "ask-around": {
      "command": "/abs/path/to/ask-around/.venv/bin/python",
      "args": ["-m", "askaround.mcp_server"],
      "env": {
        "BELWEAVE_API_KEY": "${BELWEAVE_API_KEY:-}",
        "ASKAROUND_SIGNING_KEY": "${ASKAROUND_SIGNING_KEY:-}"
      }
    }
  }
}
```

Then ask Claude Code: *"Use ask_around with roster /abs/path/roster.yaml to get approval before booking the Dallas trip."* Set `ASKAROUND_DRY_RUN=1` in `env` to rehearse with fake answers. stdout is reserved for the protocol, so the board is off in MCP mode and logs go to stderr. Any internal failure is returned as `outcome: "error"`, never as an approval.

## Safety and secrets

- `BELWEAVE_API_KEY` and `ASKAROUND_SIGNING_KEY` come from the environment and are never printed or logged.
- Webhook URLs (the token is in the path) live only in `roster.yaml` (git-ignored). They are hidden from `repr`, never sent to Kai-4, and the `httpx` loggers are pinned to WARNING so request URLs can't leak into logs. A test covers this. `receipt*.json` is git-ignored too.
- Notify 429 → wait `retryAfterSeconds` (default 60). 400 → the `issues` array is logged. Cancel 404 (already settled) is fine.
- Kai-4: `max_tokens` 4000 (hidden reasoning needs ≥ 2000), at most 2 concurrent requests, JSON-only output with code fences stripped.
- Live Activity is best effort. One start per run (Notify allows one fresh start per 60 s per device), PATCH updates are coalesced, and every error is swallowed. It can never break or alter the approval flow.

## Tests

```bash
pytest -q
```

A fake Notify transport (`askaround/fake.py`, built only from the documented endpoints) drives the suite: quorum 2/2 passes, one deny vetoes and cancels the rest, expiry escalates then blocks, dependencies wait and block, 40 random status sequences produce no false approvals, the wrong-person guard, re-plan add/edit/drop/validation/limits, Live Activity start-once/update/end and failure isolation, the policy compiler, signing and tamper detection, and a real MCP stdio round trip.

## Honest limits

- **Per-person services are required for strict routing.** On a shared Family or workplace service, Notify lets any member answer. The `user_id` guard then ignores wrong-person answers (when the response includes `respondingUserId`), but the question is still *visible* to the others. For strict "only the owner sees it", give each person their own service.
- **Live Activity delivery isn't guaranteed to be instant.** `accepted` from Notify is not proof it is visible on the phone. If it doesn't render, the board and the push notifications carry the flow.
- **Not yet exercised against live Notify and Kai-4 by the author.** Everything is built to the documented API and tested against a fake transport. Run a live task with real phones before relying on it, and expect rough edges in response-shape handling.
- **The planner is an LLM.** Expression rules like "money > 200" are applied by Kai-4, so they can be missed. Keyword rules are enforced in code, and every plan is validated, but review the plan on the board. A re-plan can only tighten pending decisions and drop informational text ones (see above). Expression rules still depend on Kai-4 for *new* decisions it adds.
- Text replies from people flow into the re-planner prompt (truncated to 300 chars). Treat them as untrusted input. A re-plan can't edit settled decisions or skip validation.
- Polling (every 2 s) instead of callbacks. Quorum met means the other asks are canceled (see consent rule).
- The escalation prompt follows PRD F6 ("Ask again?"). The PRD §3 demo wording ("Remind her or cancel?") would be a one-line change in `engine.py`.

## Prior art (honest)

Multi-human approval already exists: **HumanWire**, **Quorum**, and **Microsoft Copilot Studio multistage approvals** all do versions of it. None of them are built on Notify. Ask Around is not new as an idea. The claim is narrower: agents on Notify need this, Notify doesn't have it, and here it is as a single tool call that any MCP-capable agent can use.

## Design choices made while building

- PRD §7 is the config format. Older flat keys and string rules still load.
- `text` decisions are satisfied by a non-empty reply and recorded as information, not consent (see above).
- Only `text` decisions can be dropped by a re-plan. Dropped ones don't count against the final `approved`, and a re-plan that leaves a dangling dependency on one is rejected.
- An escalation ask to a backup is not a vote. It never appears in a decision's `votes` and never hides the backup's own pending ask.
- `veto_on_deny: false` is supported. A deny then just doesn't count, and the decision fails only when quorum becomes unreachable.
- If a decision is cancelled while its ask is still being delivered, Ask Around waits for delivery, records the eventId, then cancels it, so no live prompt is left on a phone.
- Pending polls tolerate 3 consecutive read errors, and the 4th is treated as NO. A pending ask that never settles server-side is treated as expired 45 s after its deadline.

## Next steps

- A hosted Notify web app (`appId`) that opens the decision board from a tap.
- Reply-webhook callbacks instead of polling.
- Public-key receipt signatures for third-party audit.
- A conformance test run against live Notify services in CI.
