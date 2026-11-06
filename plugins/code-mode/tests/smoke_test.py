"""Smoke test: starts a mock MCP server and checks mcpx and run.py end to end.

    python plugins/code-mode/tests/smoke_test.py

Needs Python 3.11+ and mcp 2.x (pip install "mcp>=2,<3"). Exits non-zero on failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
SKILL = PLUGIN / "skills" / "code-mode"
MOCK = Path(__file__).resolve().parent / "mock_server.py"


def run(args: list[str], stdin: str | None = None, env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run([sys.executable, *args], input=stdin, capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=180)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"mock": {"command": sys.executable, "args": [str(MOCK)]}}}))
        env = {**os.environ, "MCPX_CONFIG": str(cfg)}
        mcpx, runpy = str(SKILL / "mcpx.py"), str(SKILL / "run.py")
        script = (
            "from mcpx import server, ToolError\n"
            "crm = server('mock')\n"
            "n = sum(1 for a in crm.list_accounts(segment='Enterprise') if crm.get_invoices(account_id=a['account_id']))\n"
            "print('calls_ok', n)\n"
            "print('hyphen_ok', crm.get_summary()['accounts'])\n"
            "try:\n    crm.fails()\nexcept ToolError:\n    print('tool_error_ok')\n"
        )
        checks = [
            ("servers lists mock", run([mcpx, "servers"], env=env), lambda c, o: c == 0 and "mock" in o),
            ("tools shows signatures", run([mcpx, "tools", "mock"], env=env),
             lambda c, o: c == 0 and "get_invoices(account_id: int) -> list[{invoice_id" in o),
            ("call returns JSON", run([mcpx, "call", "mock", "get-summary"], env=env),
             lambda c, o: c == 0 and '"accounts":200' in o),
            ("tool error exits 1", run([mcpx, "call", "mock", "fails"], env=env), lambda c, o: c == 1),
            ("script via run.py", run([runpy, "-"], stdin=script, env=env),
             lambda c, o: c == 0 and "calls_ok 100" in o and "hyphen_ok 200" in o and "tool_error_ok" in o),
            ("output is capped", run([runpy, "--max-chars", "200", "-"], stdin="print('x' * 5000)", env=env),
             lambda c, o: c == 0 and "[capped:" in o and len(o) < 500),
            ("exit code reported", run([runpy, "-"], stdin="raise SystemExit(3)", env=env),
             lambda c, o: c == 3 and "[exit code 3]" in o),
        ]
        failed = 0
        for name, (code, out), ok in checks:
            good = ok(code, out)
            failed += not good
            print(f"{'PASS' if good else 'FAIL'}  {name}")
            if not good:
                print("      " + out.strip().replace("\n", "\n      ")[:800])
        print(f"\n{len(checks) - failed}/{len(checks)} passed")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
