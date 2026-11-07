# Policy and roster reference

## `policy.yaml`

```yaml
rules:
  - match: "money > 200"
    owners: [mom, dad]
    quorum: 2
defaults: { expires_s: 30, max_expires_s: 30, veto_on_deny: true, max_reasks: 1, max_replans: 2 }
```

### `rules[]`

| Field | Type | Meaning |
|---|---|---|
| `match` | string | The condition. Shown to Kai-4 verbatim. |
| `owners` | list of roster keys | Who must be asked. Unknown owners are an **error** at startup. |
| `quorum` | int, optional | Must be `1..len(owners)`, or startup fails. |

A plain string rule (`- "be careful with money"`) is also accepted. It goes to the planner with no owners or quorum.

Two kinds of rule:

- **Expression rules** contain an operator (`< > = ! $`) or the words over, under, more than, less than. Kai-4 applies them while planning. They are *not* enforced in code.
- **Keyword rules** (like `hotel`) are also enforced in code after planning and re-planning: any decision whose id or question contains the keyword (case-insensitive) gets the rule's owners added and its quorum raised to at least the rule's. Rules only tighten.

### `defaults`

| Field | Default | Meaning |
|---|---|---|
| `expires_s` | 300 | How long an ask stays open when the planner gives none (30–86400). Stored as `default_expires_s`. |
| `max_expires_s` | none | Clamp **every** decision to at most this (30–86400), after planning and re-planning. |
| `veto_on_deny` | `true` | Any deny fails the decision at once. |
| `max_reasks` | 1 | Re-asks of a silent owner after the backup says yes (0–5). |
| `max_replans` | 2 | Re-plan budget. Values above 2 are clamped to 2. |
| `require_all_owners` | `false` | Force quorum = every owner of every decision. |

The flat form (`max_reasks: 2` at the top level) also loads. `--expires N` on the CLI overrides every decision's `expires_s` at runtime (30–86400).

Policy fails closed: a policy path that doesn't exist is an error, and so is a rule naming an owner not in the roster. With no `--policy` the CLI uses `./policy.yaml` if present; otherwise it runs on built-in defaults and prints a warning.

### Examples

**Money over $200 needs both parents**

```yaml
rules:
  - match: "money > 200"
    owners: [mom, dad]
    quorum: 2
```

**A hotel needs Dad** (enforced in code, since `hotel` is a keyword)

```yaml
rules:
  - match: "hotel"
    owners: [dad]
    quorum: 1
```

**Keep silence short on stage**

```yaml
defaults: { expires_s: 30, max_expires_s: 30 }
```

### Compile from English

```bash
python -m askaround policy "budget over \$200 needs both parents" --roster roster.yaml   # writes policy.yaml
```

Kai-4 returns the same shape. Owners are checked against the roster and quorum against owner count. An existing file is saved as `policy.yaml.bak`. Use `-o -` to print instead.

## `roster.yaml`

```yaml
requester: arav
people:
  arav: { role: "requester", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: null }
  dad:  { role: "parent · budget, hotel", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
  mom:  { role: "parent · budget", webhook: "https://notify.belweave.ai/hooks/whk_…", backup: arav }
```

| Field | Meaning |
|---|---|
| `webhook` (alias `webhook_url`) | The person's own Notify webhook. A secret. |
| `role` | Used in the push title: `Ask Around · <role>`. |
| `backup` | Roster key asked when this person is silent. |
| `name` | Display name. Defaults to the key, title-cased. |
| `user_id` | Wrong-person guard: ignore answers with a different `respondingUserId`. |
| `device_id` | Adds `"deviceIds": ["<id>"]` to this person's asks. |
| `requester` (top level) | Whose service hosts the Live Activity. |

`roster.yaml` is git-ignored. Copy `roster.example.yaml` and fill in real values.
