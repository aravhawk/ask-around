# Receipts

Every run writes a JSON receipt (`receipt.json` from the CLI via `--receipt`, `receipt-<runId>.json` from MCP). `receipt*.json` is git-ignored because it contains answers and text.

## Format

| Field | Meaning |
|---|---|
| `task`, `runId`, `startedAt`, `finishedAt` | The request and timing (UTC ISO 8601). |
| `plan` | The initial validated plan: `decisions[]` with `id, question, type, owners, quorum, depends_on, expires_s`. |
| `replans[]` | One entry per re-plan: `after`, `applied`, `added`, `edited`, `dropped`, `skipped`, `proposed`, `reason`, `summary`. Only applied changes appear in `added/edited/dropped`. |
| `decisions[]` | `id, question, owners, quorum, dependsOn, outcome, votes, reason`. `outcome` is `passed`, `failed`, `blocked` or `dropped`. `votes` maps owner → `yes`, `no`, `blocked`, `cancelled`, `not asked` or `pending`. |
| `asks[]` | Every push: `decision, person, kind, prompt, eventId, status, respondedAt, text, sentAt, note`. `kind` is `primary`, `reask` or `escalation`. |
| `outcome` | `approved` (every non-dropped decision passed), `rejected` (any failed), else `blocked`. |
| `signature` | Present only when signed: `{"alg": "HMAC-SHA256", "value": "<hex>"}`. |

Ask `status` is Notify's status (`approved`, `denied`, `yes`, `no`, `replied`, `expired`, `canceled`), or `error`, or `ignored_wrong_responder`. `prompt` is the exact text sent.

## Signing

Set a key and receipts are signed automatically:

```bash
export ASKAROUND_SIGNING_KEY="a long random string"
python -m askaround "..." --roster roster.yaml
```

The signature is HMAC-SHA256 over the canonical JSON of the receipt without its `signature` block: keys sorted, separators `,` and `:`, ASCII-escaped. The key is read from the environment, never stored in the receipt, and never printed.

## Verify

```bash
python -m askaround verify receipt.json      # same ASKAROUND_SIGNING_KEY in the environment
```

| Exit code | Meaning |
|---|---|
| 0 | signature valid |
| 1 | tampered, signed with a different key, or not signed |
| 2 | no key set, or the file can't be read |

Comparison is constant-time. Changing any ask, status or outcome breaks the signature. This is tamper evidence for whoever holds the shared key. It is not public-key non-repudiation.
