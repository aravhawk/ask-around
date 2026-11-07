# PRD: Ask Around
**Who decides, how many must agree, and what happens when nobody answers: governance for agents that ask people.**
Notify Weekend 01 · Built with Notify + Belweave Kai-4 · Deadline 2026-11-07 2:00 PM CT

---

## 1. One-liner
Ask Around is a tool any agent can call, `ask_around(task, roster, policy)`. It breaks a task into decisions, sends each one through Notify to the person who owns it, enforces N-of-M approval, rejects answers from the wrong person, escalates silence, re-plans from the answers, and writes a signed decision receipt.

## 2. The problem (verified against the live Notify docs, Nov 7 2026)
Notify settles every question on the **first** answer it gets:
- Family: *"Any accepted family member … may respond, and the first valid server-confirmed answer settles the prompt. `respondingUserId` identifies that person."* (docs/family)
- Workplace groups: *"Group questions settle on the first valid answer from a current group member."*
- `deviceIds` only reaches the sender's own devices: *"A foreign or unknown selection returns `400 Invalid device selection`."*
- Families get `403` on device groups, paging, and rotations.

That's right for "is the stove off?" But agents now ask about money, access, and plans. Those decisions need:
1. **The right person:** only the budget owner can approve the budget.
2. **Enough people:** "both parents" or "2 of 3 founders."
3. **Safe silence:** no answer must never mean yes, and someone has to be told.
4. **Memory:** the agent re-plans as answers come in.

Notify can't express any of these today. Ask Around adds them on top of the primitives Notify already has.

**Honest positioning:** multi-human approval exists elsewhere (HumanWire, Quorum, Copilot Studio multistage approvals). Never claim it's new. The claim is: *"Agents on Notify need it, Notify doesn't have it, and here it is in one tool call."*

## 3. Demo scenario (90 seconds, recorded)
Prompt: **"Plan a $600 weekend trip to Dallas for the Karan Aujla concert."**

1. Kai-4 returns a plan of decisions:
   - `dates` → Arav (yes/no: "Fri–Sun Feb 5–7?")
   - `budget` → **Mom AND Dad, quorum 2 of 2** (approval: "$600 trip budget?")
   - `hotel` → Dad (text: "Hotel preference near AAC?"), only after the budget passes
2. Terminal shows a live decision board. The requester's phone shows one Live Activity: *"Dallas trip · 0 of 3 decided."*
3. Dad (friend's phone) taps **Approve** → board: budget 1/2. Live Activity: *"Waiting on Mom."*
4. Mom stays silent → her ask expires (30 s in the demo) → **escalation**: Arav gets *"Mom hasn't answered the $600 budget. Remind her or cancel?"* Say out loud: **"Silence didn't become a yes."**
5. Arav taps Remind → a new ask goes to Mom → she (or a stand-in) approves → quorum met → **re-plan**: Kai-4 unlocks the hotel decision and sends it to Dad.
6. Dad replies "Under $150, walkable" → plan complete → Live Activity ends: *"Approved by Dad + Mom · receipt saved."*
7. Show `receipt.json`: who decided what, when, the exact prompt text, and the Notify event IDs.

Close: *"That was one tool call. Any agent on Notify can use it: Claude Code, Codex, or your own."*

## 4. Users and roles
- **Requester:** the person or agent that starts a task (Arav).
- **Roster member:** a person with a name, a role, and **their own Notify webhook service URL**. Each person creates a service in their own Notify account (Dashboard → Services) and shares the URL. This is what makes per-person routing work without Family or workplace features.
- **Backup:** a roster member who gets escalations for someone else.

## 5. Functional requirements

### P0: must ship (core, 10–20 min)
| # | Requirement | Notes |
|---|---|---|
| F1 | Load `roster.yaml` and `policy.yaml` | See §7 |
| F2 | **Plan:** Kai-4 turns task + roster into JSON decisions `{id, question, type: approval|yes_no|text, owners[], quorum, depends_on[], expires_s}` | Validate the JSON; retry once on a parse failure |
| F3 | **Ask:** for each ready decision, POST to each owner's webhook with `title` = "Ask Around · {role}", `body`, `response {type, expiresInSeconds, correlationId}` | Keep an `Idempotency-Key` per ask |
| F4 | **Collect:** poll `GET /hooks/:token/events/:eventId` every 2 s per pending ask | Terminal states: approved, denied, yes, no, replied, expired, canceled |
| F5 | **Quorum:** a decision passes when `quorum` owners approve or say yes. **Any deny fails it at once** (veto). Then cancel the other pending asks via `POST /events/:id/cancel` | |
| F6 | **Silence policy:** `expired` is never approval. On expiry, escalate to the owner's `backup` with a yes/no: "{owner} hasn't answered '{question}'. Ask again?" Yes → re-ask the owner once; no → mark the decision `blocked` | |
| F7 | **Dependencies:** a decision is ready only after everything in `depends_on` has passed | |
| F8 | **Re-plan:** after each decision settles, send Kai-4 the state and let it add, drop, or edit pending decisions (it can't edit settled ones) | Keep a max of 2 re-plans to bound cost |
| F9 | **Board:** a live terminal table showing each decision, its owners, votes, status, and time left | `rich` library |
| F10 | **Receipt:** `receipt.json` with every decision, prompt text, eventIds, who answered, the outcome, and timestamps | |

### P1: ship if time allows (each about 5–10 min)
| # | Requirement |
|---|---|
| F11 | **Live Activity** on the requester's service: start once (`POST /live-activities`, `key: task-id`), `PATCH` the status on every change ("2 of 3 decided · waiting on Mom", progress 0–1), `POST /end` at the finish. Respect the 1 start per 60 s per device limit. Never claim it's instant. |
| F12 | **MCP server** exposing `ask_around(task, roster_path, policy_path)` so Claude Code or Codex can call it. This is the "any agent" claim, made real. |
| F13 | **Wrong-person guard** for a shared Family service: when `respondingUserId` isn't one of the decision's owners, ignore the answer and re-ask. (Per-person services make this impossible by design, so call that out in the README.) |
| F14 | **Plain-English policy:** "budget over $200 needs both parents" → Kai-4 compiles it into policy.yaml. |

### P2: pitch only (mention as next steps, don't build)
- Signed receipts (HMAC over the decision log) for audits.
- A hosted Notify web app (`appId`) that opens the decision board from a tap.
- Reply-webhook callbacks instead of polling.

## 6. Non-functional requirements
- **Safety rule (headline):** only a server-confirmed `approved` or `yes` counts as consent. `expired`, `canceled`, network errors, and queued answers all count as **no**. This is Notify's own rule, extended to groups.
- Kai-4: `max_tokens ≥ 2000` (hidden reasoning; lower values return empty content). Limits: 60 rpm, 2 streams.
- Notify: there's no monthly cap, but per-minute rate limits apply (on 429, back off `retryAfterSeconds`). `body` is at most 2000 characters when it carries `response`; `title` at most 80; `expiresInSeconds` 30–86400.
- Secrets come from env vars: `BELWEAVE_API_KEY`, and webhook URLs in roster.yaml (gitignored). Never print a token.

## 7. Config formats
```yaml
# roster.yaml (gitignored; webhook URLs are secrets)
people:
  arav: { role: "requester", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: null }
  dad:  { role: "parent · budget, hotel", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
  mom:  { role: "parent · budget", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
requester: arav
```
```yaml
# policy.yaml
rules:
  - match: "money > 200"   # Kai-4 applies these while planning
    owners: [mom, dad]
    quorum: 2
  - match: "hotel"
    owners: [dad]
    quorum: 1
defaults: { expires_s: 30, veto_on_deny: true, max_reasks: 1, max_replans: 2 }
```

## 8. Architecture
```
task ─▶ Planner (Kai-4) ─▶ Decision graph ─▶ Dispatcher ─▶ Notify webhooks (one per person)
             ▲                    │                 │
             └── Re-planner ◀── State store ◀── Poller (GET events)
                                  │
                       Board (rich) · Live Activity · receipt.json
```
- Single Python file is fine (`askaround.py`, about 300 lines): `httpx` + `rich` + `pyyaml`. Or TypeScript plus notify-tools if your agent prefers it.
- Belweave: `POST https://api.infer.belweave.ai/v1/chat/completions`, model `Belweave/Kai-4`, bearer auth, OpenAI-compatible.
- Notify base: `https://notify.belweave.ai/hooks/:token`.

### Kai-4 planner prompt (starting point)
> You are a decision planner. Given a TASK, a ROSTER (names and roles), and a POLICY, output ONLY JSON: {"decisions":[{"id","question" (≤200 chars, standalone, specific numbers),"type":"approval|yes_no|text","owners":[names],"quorum":int,"depends_on":[ids],"expires_s":int}]}. Ask the fewest people possible. Never ask someone outside the roster. Apply every policy rule. Put irreversible decisions after cheap ones.

Re-planner: same prompt plus the STATE (settled decisions and their answers). Output only the changes to pending decisions.

## 9. Build order (to finish by 2:00 PM CT)
1. **12:50–1:05**: F1–F5, F9: plan, ask, poll, quorum, board. Test with 2 services on your own phone (titles "Mom"/"Dad").
2. **1:05–1:15**: F6–F8, F10: escalation, dependencies, re-plan, receipt.
3. **1:15–1:25**: F11 Live Activity; F12 MCP if the agent is fast.
4. **1:25–1:40**: friend's phone plays Dad (his own Notify account plus a service URL). Record the §3 script.
5. **1:40–1:55**: README (§10) + submit. **Leave 5 minutes of buffer.**

## 10. README skeleton (judges read this)
1. The 3 doc quotes from §2, then: "Ask Around adds who, how many, and what silence means."
2. A GIF or video of the demo.
3. "Consent rule: only a server-confirmed approve counts. Timeout, cancel, error = no."
4. Quickstart: `pip install …`, create a Notify service per person, fill roster.yaml, `python askaround.py "task"`.
5. MCP: `ask_around` tool config for Claude Code.
6. Honest limits: Live Activity delivery isn't guaranteed to be instant; per-person services are required for strict routing; prior art exists (named).
7. Next: signed receipts, a hosted board via `appId`, reply webhooks.

## 11. Success criteria
- Live demo shows: right-person routing, 2-of-2 quorum, a veto or escalation on silence, one re-plan, and a receipt.
- Zero false approvals in any path.
- Judge recognizes the gap within 10 seconds of the pitch.

## 12. Risks and fallbacks
| Risk | Fallback |
|---|---|
| Friend's phone unavailable | Two services on one phone, titled "Mom" and "Dad." Say so honestly. |
| Live Activity doesn't render | Cut it. The board + push notifications carry the demo. |
| Kai-4 slow or empty | `max_tokens` 4000; cache the plan from a dry run for the recording |
| Rate limit (429) | Back off `retryAfterSeconds`; the demo uses fewer than 15 calls |
