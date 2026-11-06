---
name: code-mode
description: Cut token use by writing a script that calls MCP tools, CLIs or APIs and prints only the answer, instead of calling tools one at a time and reading every result. Use when a task needs many tool calls, loops over items, joins data from several sources, or reads large results to get a small answer (counts, totals, mismatches, top N, "which X have Y"). Includes mcpx, a bridge that makes any configured MCP server callable from Python. Skip it for one or two calls.
---

# Code mode

The model writes a script, the script calls the tools, and only what it prints comes back.
The data stays in the script, so it never fills the context or gets re-read on later turns.

Tools (use `python3` if `python` is not Python 3.11+ on this machine):
- `python "${CLAUDE_SKILL_DIR}/mcpx.py"`: MCP bridge (servers, tools, schema, call)
- `python "${CLAUDE_SKILL_DIR}/run.py"`: runs a script with `mcpx` importable; caps output

## When to use it

Use it when the task needs about 3 or more calls, or results much bigger than the answer: loops
over accounts, issues, files or resources; joins across systems; audits; "how many / which / total".
Don't use it for one or two calls, or for a file you are about to edit. Normal tool use is cheaper there.

## Workflow

1. **Find the tools.** Don't load full schemas.
   ```bash
   python "${CLAUDE_SKILL_DIR}/mcpx.py" servers
   python "${CLAUDE_SKILL_DIR}/mcpx.py" tools <server> [filter]   # name(arg, opt?) -> returns
   python "${CLAUDE_SKILL_DIR}/mcpx.py" schema <server> <tool>    # only if the signature isn't enough
   ```
   For non-MCP sources, use their CLI or API inside the script (`gh api`, `aws`, `kubectl`, `wrangler`,
   `az`, `curl`, `git`) through `subprocess`, reading JSON output where the CLI offers it.
2. **Check the shape if the return type isn't shown.** Make one call and print just the keys or the first record:
   `mcpx.py call <server> <tool> '{"id": 1}' --max-chars 600`.
3. **Write the script and run it in one step** (stdin, so no file is needed):
   ```bash
   python "${CLAUDE_SKILL_DIR}/run.py" - <<'EOF'
   from mcpx import server
   crm, billing = server("crm"), server("billing")
   at_risk = []
   for a in crm.list_accounts(segment="Enterprise"):
       inv = billing.get_invoices(account_id=a["id"])
       overdue = sum(i["amount"] for i in inv if i["days_overdue"] > 60)
       if overdue:
           at_risk.append((a["name"], overdue))
   at_risk.sort(key=lambda x: -x[1])
   print(f"accounts={len(at_risk)} total={sum(x[1] for x in at_risk):.2f}")
   for name, amt in at_risk[:10]:
       print(f"{name}\t{amt:.2f}")
   EOF
   ```
   In PowerShell, pipe a here-string instead: `@'` … `'@ | python "${CLAUDE_SKILL_DIR}/run.py" -`.
   For longer scripts, or ones you will rerun, write them to the session scratchpad and run
   `run.py <path>`. Never put them in the user's repo.

## Library

- `server(name)` connects once per process. Tools are methods: `srv.get_invoices(account_id=1)`.
  Hyphenated names become underscores (`get-summary` → `srv.get_summary()`). `srv.call("exact-name", {...})` also works.
- Results come back as Python objects: structured content, parsed JSON text, or plain strings.
- Tool failures raise `mcpx.ToolError`. Connection or config problems raise `mcpx.MCPXError`. Catch
  `ToolError` per item in loops so one bad record doesn't lose the whole run.
- `srv.tools()` lists names. `mcpx.signature(t)` shows one, for each `t` in `srv.tool_list()`.

## Rules

- **Print conclusions, not data.** Counts, totals, mismatches, IDs, and the top N with only the needed fields.
  `run.py` caps output at 4000 chars (`--max-chars N` to change it). If you hit the cap, aggregate more; don't raise it.
- **Write code against the documented shape.** If a key might be missing, use `.get()`, and print a
  short note about records that didn't fit instead of crashing.
- **Read-only by default.** Only call tools that change things (create, update, delete, send) when the
  user asked for that change. Print what was changed.
- **Never print secrets,** tokens, env values or full config entries.
- **If a script fails, fix it and rerun.** The traceback is in the capped output. Don't fall back to
  calling the same tools one by one unless the bridge can't reach the server.

## Limits

- **Reachable:** stdio servers, and http/sse servers using header or token auth from `~/.claude.json`,
  `.mcp.json` or `$MCPX_CONFIG`. `${VAR}` and `${VAR:-default}` are expanded.
- **Not reachable:** servers that use Claude Code's OAuth login, claude.ai connectors, browser
  extensions, and plugin-bundled servers. For those, call the tools directly. For a plugin server
  with token auth, write a `{"mcpServers": {...}}` file and set `MCPX_CONFIG` to its path.
- **Stdio servers are launched as a second copy** for the script. Avoid this for servers that lock a file or port.
- **Tools that ask for user input** (elicitation) mid-call fail in scripts. Call them directly.
- **Timeouts:** each call times out at 300s (`MCPX_CALL_TIMEOUT`), connecting at 60s
  (`MCPX_CONNECT_TIMEOUT`), and the whole script at 600s (`run.py --timeout`).
- **Logs:** stdio server stderr goes to `<temp>/mcpx-logs/<server>.log`, which the connection error names.
- **Requires** Python 3.11+ and `pip install "mcp>=2,<3"`. mcpx says so if either is missing.
