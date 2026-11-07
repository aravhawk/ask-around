# MCP server

`python -m askaround.mcp_server` (or `askaround mcp`) is a stdio MCP server built on the official `mcp` Python SDK (v1 `FastMCP` and v2 `MCPServer` are both supported). stdout carries the protocol, so the live board is off and logs go to stderr.

## Setup

```bash
claude mcp add --env BELWEAVE_API_KEY="$BELWEAVE_API_KEY" ask-around -- \
  /abs/path/ask-around/.venv/bin/python -m askaround.mcp_server
```

or `.mcp.json`:

```json
{ "mcpServers": { "ask-around": {
    "command": "/abs/path/ask-around/.venv/bin/python",
    "args": ["-m", "askaround.mcp_server"],
    "env": { "BELWEAVE_API_KEY": "${BELWEAVE_API_KEY:-}", "ASKAROUND_SIGNING_KEY": "${ASKAROUND_SIGNING_KEY:-}" } } } }
```

Environment: `BELWEAVE_API_KEY` (planner), `ASKAROUND_SIGNING_KEY` (signs receipts), `ASKAROUND_DRY_RUN=1` (fake Notify answers, for rehearsal).

## Tool: `ask_around`

| Input | Type | Meaning |
|---|---|---|
| `task` | string | What the agent wants approved or planned. |
| `roster_path` | string | Path to `roster.yaml`. Must exist. |
| `policy_path` | string, default `""` | Path to `policy.yaml`. Empty means `./policy.yaml` if present. A path that doesn't exist is an error. |

**Output:** one text content block holding the receipt as JSON ([receipts.md](receipts.md)). The receipt is also written to `receipt-<runId>.json` in the server's working directory.

**Always check `outcome`.** `approved` means every decision passed on server-confirmed answers. `rejected` and `blocked` mean do not proceed. Failures return `{"outcome": "error", "error": "..."}` and are never an approval. The call blocks until the decisions settle, which can take as long as people take to answer (bounded by each decision's `expires_s`).

## Example

Call:

```json
{ "name": "ask_around",
  "arguments": { "task": "Plan a $600 weekend trip to Dallas for a concert",
                 "roster_path": "/abs/path/roster.yaml", "policy_path": "/abs/path/policy.yaml" } }
```

Response (trimmed, from a `--dry-run`):

```json
{ "task": "Plan a $600 weekend trip to Dallas for a concert",
  "runId": "bf5ea4857e",
  "outcome": "approved",
  "decisions": [{ "id": "d1", "owners": ["dad", "mom"], "quorum": 2, "outcome": "passed",
                  "votes": { "dad": "yes", "mom": "yes" }, "reason": "" }],
  "asks": [{ "decision": "d1", "person": "dad", "kind": "primary", "eventId": "evt_1", "status": "approved" }] }
```

Tell the calling agent in its instructions: *proceed only when `outcome == "approved"`.*
