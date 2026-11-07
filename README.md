# Ask Around

**Governance for agents that ask people. Who may answer, how many must agree, and what silence means.**

🎥 Demo video: _link coming soon_

One tool call, `ask_around(task, roster, policy)`, and an agent gets real humans to decide through [Notify](https://notify.belweave.ai). Each question goes to the person who owns it. Quorum and veto are enforced. Silence is never a yes.

## The problem

Notify's own docs say how a question settles:

> Family: "Any accepted family member … may respond, and the first valid server-confirmed answer settles the prompt."

> `deviceIds` only reach your own devices: "A foreign or unknown selection returns `400 Invalid device selection`." Family accounts get `403` on device groups.

That is right for "is the stove off?". It is wrong for money. A $600 question can be approved by **whoever taps first**, and a timeout is just an **expiry**.

## What Ask Around adds

| | Notify today | Ask Around |
|---|---|---|
| **Who answers** | Anyone who can see the prompt | The owner only, routed per person (one webhook service each, optional `device_id`) |
| **How many agree** | First tap settles it | N-of-M quorum, and any deny vetoes |
| **Silence** | Expires | Never consent: it escalates to a backup, then blocks |

## How it works

```
task ─▶ Kai-4 planner ─▶ decision graph (owners, quorum, depends_on)
                                │  independent decisions run in parallel
                                ▼
        Notify push to each owner ─▶ quorum / veto engine ─▶ re-plan (max 2) ─▶ signed receipt.json
              ▲ silence: ask the backup "Ask again?" ─────┘
```

- **Planner:** Belweave Kai-4 splits the task into decisions. The output is validated (owners must be in the roster, quorum must be satisfiable, no dependency cycles). It never sees your webhook URLs.
- **Engine:** polls every 2 s. A decision passes at `quorum` yes votes. Any deny fails it at once and cancels the other pending asks.
- **Silence:** an expired ask goes to the owner's backup as "X hasn't answered. Ask again?". Yes re-asks once. Anything else marks the decision `blocked`, and so is everything that depends on it.
- **Re-plan:** after a decision settles, Kai-4 may add, edit or drop pending decisions. A re-plan can only **tighten** governance, never loosen it.
- **Policy:** write `policy.yaml` by hand, or compile it from English: `python -m askaround policy "budget over \$200 needs both parents"`.
- **Receipt:** `receipt.json` records every ask (person, exact prompt, event ID, status, time, text) and the outcome. Set `ASKAROUND_SIGNING_KEY` and it is HMAC-SHA256 signed. Check it with `python -m askaround verify receipt.json`.
- **Also:** a live terminal board, a Live Activity on the requester's phone (best effort), a wrong-person guard for shared Family services, and free-text decisions (like "hotel preference?") that count as information, never consent.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"

# One Notify webhook service per person, created in that person's own account.
cp roster.example.yaml roster.yaml     # git-ignored: webhook URLs are secrets
export BELWEAVE_API_KEY=...            # the only env var the planner needs

python -m askaround "Plan a \$600 weekend trip to Dallas for a concert" \
  --roster roster.yaml --policy policy.yaml --expires 30
```

No phones? Add `--dry-run`. It fakes Notify, and one person goes silent on their first ask so you can watch the escalation.
Exit codes: `0` approved, `1` rejected or blocked, `2` error.

## MCP: let any agent call it

The stdio server exposes one tool, `ask_around(task, roster_path, policy_path)`, and returns the receipt as JSON. Only `outcome == "approved"` means go.

```bash
claude mcp add --env BELWEAVE_API_KEY="$BELWEAVE_API_KEY" ask-around -- \
  /abs/path/ask-around/.venv/bin/python -m askaround.mcp_server
```

Or in `.mcp.json`: `{"mcpServers": {"ask-around": {"command": "/abs/path/.venv/bin/python", "args": ["-m", "askaround.mcp_server"]}}}`

## Safety guarantees

- **A timeout is never approval.** Only a server-confirmed `approved` or `yes` counts. `expired`, `canceled`, `denied`, network errors, malformed responses and unknown statuses all count as NO.
- **Fails closed.** A policy rule that names an owner who isn't in the roster is an error. So is a policy path that doesn't exist.
- **Re-plans can't loosen anything.** They can't lower quorum, remove owners or dependencies, or turn an approval into free text, and only informational decisions can be dropped. A re-plan that breaks a rule is rejected whole.
- **No dangling prompts.** When a decision settles, its other pending asks are cancelled, including one still being delivered.
- **Secrets stay put.** API and signing keys come from the environment. Webhook URLs live only in the git-ignored `roster.yaml`. They are never sent to the planner or logged.
- **`--expires` / `max_expires_s`** clamp every decision's timeout, so silence escalates in seconds, not minutes.

## Tests

`pytest -q` runs **122 tests** against a fake Notify transport built from the documented endpoints. They cover quorum, veto, escalation, dependencies, re-plan limits, Live Activity failures, signing and tamper detection, a real MCP stdio round trip, and 40 random status sequences that must never produce a false approval.

Built in 2 hours for **Notify Weekend 01** with Notify and Belweave Kai-4.

## Honest notes

- Multi-human approval already exists elsewhere (HumanWire, Quorum, Copilot Studio multistage approvals). Ask Around is not a new idea. It brings it to Notify, as one tool call any agent can use.
- Strict "only the owner sees it" needs per-person services. On a shared service, the wrong-person guard ignores bad answers, but others can still see the prompt.
- Live Activity delivery isn't guaranteed to be instant, and the planner is an LLM: rules like "money > 200" are applied by Kai-4, while plain-keyword rules are also enforced in code.
- Quorum met means the remaining asks are cancelled, so a later deny in a 1-of-2 decision is never seen. Use quorum = all owners when everyone must agree.
