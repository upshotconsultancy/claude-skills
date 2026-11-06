"""run.py: run a Python script with mcpx importable, and cap what comes back.

    python run.py [--max-chars N] [--timeout S] script.py [args...]
    python run.py [--max-chars N] - <<'EOF'      # script on stdin, no file needed
    ...
    EOF

stdout and stderr are merged, capped (head + tail), and the exit code is reported if non-zero.
MCP servers' own stderr goes to log files, not to this output.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mcpx import DEFAULT_MAX_CHARS, cap  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    p = argparse.ArgumentParser(prog="run.py")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("script", help="path to a .py file, or - to read the script from stdin")
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args()

    tmp = None
    if a.script == "-":
        fd, tmp = tempfile.mkstemp(prefix="codemode-", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(sys.stdin.read())
        script = tmp
    else:
        script = a.script

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH")]))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.run([sys.executable, script, *a.args], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=a.timeout)
        out, code = proc.stdout.decode("utf-8", "replace"), proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") + f"\n[run.py: killed after {a.timeout}s]"
        code = 124
    finally:
        if tmp:
            os.unlink(tmp)

    print(cap(out.rstrip("\n"), a.max_chars) or "[no output]")
    if code:
        print(f"[exit code {code}]")
    return code


if __name__ == "__main__":
    sys.exit(main())
