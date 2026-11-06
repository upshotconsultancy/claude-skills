# Code mode for Claude Code

When Claude calls a tool, it reads every result that tool returns, and you pay for those tokens on every
later turn as well. With code mode, Claude writes a short script instead. The script calls the tools,
the data stays in the script, and Claude reads only what the script prints.

This plugin adds a `code-mode` skill with two small Python tools:

- **`mcpx.py`**: a bridge that makes any MCP server you have configured callable from Python
  (`server("crm").get_invoices(account_id=1)`). It also prints short tool signatures in place of full JSON schemas.
- **`run.py`**: runs a script, with the script piped in so no file is needed, and caps what comes back.

For non-MCP sources (`gh`, `aws`, `kubectl`, `az`, `curl`, `git` and others), the skill tells Claude
to call the CLI from inside the script, with the same rule: print the answer, not the data.

Background: [Code mode: stop paying your agent to read](https://upshotconsultancy.com.au/articles/code-mode).

## Example

Asked "which enterprise accounts have invoices more than 60 days overdue?", Claude runs:

```python
from mcpx import server
crm, billing = server("crm"), server("billing")
at_risk = []
for a in crm.list_accounts(segment="Enterprise"):
    overdue = sum(i["amount"] for i in billing.get_invoices(account_id=a["id"]) if i["days_overdue"] > 60)
    if overdue:
        at_risk.append((a["name"], overdue))
print(f"accounts={len(at_risk)} total={sum(x[1] for x in at_risk):.2f}")
```

In testing against a mock server, this made 101 tool calls in under 2 seconds. Normal tool calling
would have sent Claude about 15,000 tokens of results, plus the history re-read on every turn. With
the script, Claude read one line.

## Requirements

- Claude Code
- Python 3.11 or newer
- The MCP Python SDK, version 2.x:

  ```bash
  python -m pip install "mcp>=2,<3"
  ```

## Install

### As a plugin (recommended)

In Claude Code:

```
/plugin marketplace add <github-owner>/claude-skills
/plugin install code-mode@upshot
```

### By hand

Copy `plugins/code-mode/skills/code-mode/` into `~/.claude/skills/` to use it in every project,
or into `.claude/skills/` in one repo.

### Optional: make Claude reach for it

The skill loads when Claude decides a task fits. To make that more reliable, add this line to `~/.claude/CLAUDE.md`:

```markdown
- When a task needs many tool calls, or reads large results to get a small answer (counts, totals, mismatches, "which X have Y"), use the code-mode skill: write a script, and print only the answer, capped. Don't read raw output. Skip it for one or two calls, or for files you're about to edit.
```

## Check it works

```bash
python plugins/code-mode/tests/smoke_test.py
```

This starts a mock MCP server and checks the bridge, the runner, error handling and the output cap.

To try it against your own servers:

```bash
python plugins/code-mode/skills/code-mode/mcpx.py servers
python plugins/code-mode/skills/code-mode/mcpx.py tools <server>
```

## Which servers it can reach

mcpx reads servers the way Claude Code does: local scope in `~/.claude.json`, then the nearest
`.mcp.json`, then user scope in `~/.claude.json`. `${VAR}` and `${VAR:-default}` are expanded. You can add
more with `MCPX_CONFIG=/path/to/file.json` (format: `{"mcpServers": {...}}`).

| Server | Works |
|---|---|
| stdio (`command` + `args`) | Yes. mcpx starts its own copy. |
| HTTP or SSE with header or token auth | Yes |
| HTTP that uses Claude Code's OAuth login (`/mcp`) | No. Claude calls those tools directly. |
| claude.ai connectors, browser extensions | No |
| Servers bundled inside other plugins | Only if you describe them in an `MCPX_CONFIG` file |

## Limits

- **More output tokens:** Claude writes more, because a script is longer than one tool call. For one
  or two calls, normal tool use is cheaper, and the skill says so.
- **Second copy of stdio servers:** each script starts its own copy. Avoid this for servers that lock a file or port.
- **No user input:** tools that ask for input mid-call (elicitation) can't run from a script.
- **Timeouts:** 300s per call (`MCPX_CALL_TIMEOUT`), 60s to connect (`MCPX_CONNECT_TIMEOUT`),
  600s per script (`run.py --timeout`).
- **Scripts run as you,** with your permissions. The skill tells Claude to stay read-only unless you
  asked for a change, and never to print secrets. Claude Code still asks before running them, as usual.

## Licence

MIT. See [LICENSE](../../LICENSE).
