"""mcpx: call MCP servers from scripts, so tool results stay out of the model's context.

Library use (inside a script run by run.py):
    from mcpx import server
    crm = server("crm")
    rows = crm.list_accounts(segment="Enterprise")   # returns parsed JSON / Python objects
    crm.call("tool-with-dashes", {"arg": 1})         # explicit form

CLI:
    python mcpx.py servers                     list configured servers (no secrets printed)
    python mcpx.py tools <server> [filter]     compact signatures:  name(arg, opt?) -> returns
    python mcpx.py schema <server> <tool>      full description and JSON schemas of one tool
    python mcpx.py call <server> <tool> [json] call one tool, print capped result

Config is read the way Claude Code reads it (highest precedence first):
    $MCPX_CONFIG (path(s) separated by os.pathsep, each {"mcpServers": {...}})
    ~/.claude.json  projects[<this project>].mcpServers   (local scope)
    nearest .mcp.json walking up from the working directory (project scope)
    ~/.claude.json  mcpServers                            (user scope)
${VAR} and ${VAR:-default} are expanded in command, args, env, url and headers.

Supported: stdio servers, and http/sse servers that authenticate with headers.
Not supported: servers that rely on Claude Code's OAuth login, claude.ai connectors, plugins' own servers
(unless you point MCPX_CONFIG at a file describing them).
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

DEFAULT_MAX_CHARS = 4000
CALL_TIMEOUT = float(os.environ.get("MCPX_CALL_TIMEOUT", "300"))
CONNECT_TIMEOUT = float(os.environ.get("MCPX_CONNECT_TIMEOUT", "60"))
LOG_DIR = Path(tempfile.gettempdir()) / "mcpx-logs"


class MCPXError(RuntimeError):
    pass


class ToolError(MCPXError):
    """The tool ran and reported an error (is_error=True)."""


# --------------------------------------------------------------------------- output capping

def cap(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Keep the head and a short tail; say how much was dropped."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    tail = max_chars // 4
    head = max_chars - tail
    dropped = len(text) - head - tail
    return (f"{text[:head]}\n... [capped: {dropped} of {len(text)} chars omitted; "
            f"print less, or aggregate in the script] ...\n{text[-tail:]}")


# --------------------------------------------------------------------------- config

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise MCPXError(f"environment variable {name} is not set (used in MCP config)")
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"mcpx: could not read {path}: {e}", file=sys.stderr)
        return {}


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def load_config(cwd: str | None = None) -> dict[str, dict]:
    """Return {name: config} with a private '_scope' key, highest precedence wins."""
    cwd_path = Path(cwd or os.getcwd()).resolve()
    layers: list[tuple[str, dict]] = []  # lowest precedence first

    claude_json = _read_json(Path.home() / ".claude.json")
    layers.append(("user", claude_json.get("mcpServers") or {}))

    for d in [cwd_path, *cwd_path.parents]:
        f = d / ".mcp.json"
        if f.is_file():
            layers.append((f"project:{f}", _read_json(f).get("mcpServers") or {}))
            break

    projects = claude_json.get("projects") or {}
    wanted = {_norm(d) for d in [cwd_path, *cwd_path.parents]}
    # the most specific (longest) matching project path wins
    for key in sorted(projects, key=len):
        if _norm(key) in wanted:
            layers.append(("local", (projects[key] or {}).get("mcpServers") or {}))

    for extra in filter(None, os.environ.get("MCPX_CONFIG", "").split(os.pathsep)):
        layers.append((f"MCPX_CONFIG:{extra}", _read_json(Path(extra)).get("mcpServers") or {}))

    merged: dict[str, dict] = {}
    for scope, servers in layers:
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                merged[name] = {**cfg, "_scope": scope}
    return merged


def _transport(cfg: dict) -> str:
    t = (cfg.get("type") or cfg.get("transport") or "").lower()
    if t in ("http", "streamable-http", "streamable_http"):
        return "http"
    if t == "sse":
        return "sse"
    if t == "stdio" or "command" in cfg:
        return "stdio"
    if "url" in cfg:
        return "http"
    return t or "unknown"


# --------------------------------------------------------------------------- background event loop

class _Runtime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="mcpx-loop", daemon=True)
        self.thread.start()
        self.servers: dict[str, Server] = {}
        atexit.register(self.close)

    def run(self, coro, timeout: float | None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise MCPXError(f"timed out after {timeout}s") from None

    def close(self) -> None:
        for s in list(self.servers.values()):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


_runtime: _Runtime | None = None
_runtime_lock = threading.Lock()


def _rt() -> _Runtime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _Runtime()
        return _runtime


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


# --------------------------------------------------------------------------- server proxy

def _require_mcp() -> None:
    if sys.version_info < (3, 11):
        raise MCPXError(f"mcpx needs Python 3.11 or newer (this is {sys.version.split()[0]})")
    try:
        from importlib.metadata import version
        major = int(version("mcp").split(".")[0])
    except Exception:  # noqa: BLE001
        raise MCPXError(f'the MCP Python SDK is not installed; run: {sys.executable} -m pip install "mcp>=2,<3"') from None
    if major < 2:
        raise MCPXError(f'mcpx needs mcp 2.x (found {version("mcp")}); run: {sys.executable} -m pip install -U "mcp>=2,<3"')


def _pyname(name: str) -> str:
    return re.sub(r"\W", "_", name)


class Server:
    """A live connection to one MCP server. Tools are available as methods."""

    def __init__(self, name: str, cfg: dict) -> None:
        _require_mcp()
        self._name = name
        self._cfg = _expand({k: v for k, v in cfg.items() if not k.startswith("_")})
        self._session = None
        self._tools: list | None = None
        self._stop: asyncio.Event | None = None
        self._task = None
        self._log = LOG_DIR / f"{_pyname(name)}.log"
        self._connect()

    # -- connection
    def _connect(self) -> None:
        ready = threading.Event()
        box: dict[str, Any] = {}

        async def hold():
            from contextlib import AsyncExitStack
            from mcp import ClientSession
            self._stop = asyncio.Event()
            try:
                async with AsyncExitStack() as stack:
                    streams = await stack.enter_async_context(self._open_transport(stack))
                    session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                    await session.initialize()
                    self._session = session
                    ready.set()
                    await self._stop.wait()
            except BaseException as e:  # noqa: BLE001
                box["error"] = _leaf(e)
                ready.set()

        self._task = asyncio.run_coroutine_threadsafe(hold(), _rt().loop)
        if not ready.wait(CONNECT_TIMEOUT):
            raise MCPXError(f"{self._name}: no response within {CONNECT_TIMEOUT}s (log: {self._log})")
        if "error" in box:
            raise MCPXError(self._explain(box["error"])) from box["error"]

    def _open_transport(self, stack):
        cfg = self._cfg
        kind = _transport(cfg)
        if kind == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            errlog = stack.enter_context(open(self._log, "a", encoding="utf-8", errors="replace"))
            params = StdioServerParameters(
                command=cfg["command"],
                args=[str(a) for a in cfg.get("args") or []],
                env={**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}},
                cwd=cfg.get("cwd"),
            )
            return stdio_client(params, errlog=errlog)
        if kind == "http":
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            client = create_mcp_http_client(headers=cfg.get("headers") or None)

            async def note_status(response):  # the SDK hides HTTP status codes in its errors
                if response.status_code >= 400:
                    self._http_status = response.status_code
            client.event_hooks.setdefault("response", []).append(note_status)
            return streamable_http_client(cfg["url"], http_client=client)
        if kind == "sse":
            from mcp.client.sse import sse_client
            return sse_client(cfg["url"], headers=cfg.get("headers") or None)
        raise MCPXError(f"{self._name}: unsupported transport {kind!r}")

    def _explain(self, e: BaseException) -> str:
        msg = f"{type(e).__name__}: {e}"
        status = getattr(self, "_http_status", None)
        if status:
            msg += f" (HTTP {status})"
        hint = ""
        if status in (401, 403) or re.search(r"\b401\b|unauthori[sz]ed|\b403\b", msg, re.I):
            hint = (" This server probably needs OAuth (a Claude Code /mcp login). mcpx only supports "
                    "header/token auth; call this server's tools directly instead.")
        elif isinstance(e, FileNotFoundError) and sys.platform == "win32":
            hint = ' On Windows, try "command": "cmd", "args": ["/c", "npx", ...].'
        log = f" (stderr log: {self._log})" if _transport(self._cfg) == "stdio" else ""
        return f"could not connect to MCP server {self._name!r}: {msg}.{hint}{log}"

    def close(self) -> None:
        if self._stop is not None and self._task is not None and not self._task.done():
            _rt().loop.call_soon_threadsafe(self._stop.set)
            try:
                self._task.result(5)
            except Exception:  # noqa: BLE001
                pass

    # -- tools
    def tool_list(self) -> list:
        if self._tools is None:
            async def fetch():
                from mcp.types import PaginatedRequestParams
                tools, cursor = [], None
                while True:
                    res = await self._session.list_tools(
                        params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                    tools.extend(res.tools)
                    cursor = res.next_cursor
                    if not cursor:
                        return tools
            self._tools = _rt().run(fetch(), CALL_TIMEOUT)
        return self._tools

    def tools(self) -> list[str]:
        return [t.name for t in self.tool_list()]

    def _resolve(self, name: str):
        tools = self.tool_list()
        for t in tools:
            if t.name == name:
                return t
        for t in tools:
            if _pyname(t.name) == _pyname(name):
                return t
        close = [t.name for t in tools if name.lower().replace("_", "") in t.name.lower().replace("_", "").replace("-", "")]
        raise MCPXError(f"{self._name} has no tool {name!r}" + (f"; did you mean {close[:5]}?" if close else
                        f"; run: mcpx.py tools {self._name}"))

    def call(self, tool: str, args: dict | None = None, /, *, timeout: float | None = None, **kwargs) -> Any:
        t = self._resolve(tool)
        arguments = {**(args or {}), **kwargs}

        async def go():
            return await self._session.call_tool(t.name, arguments)

        try:
            res = _rt().run(go(), timeout or CALL_TIMEOUT)
        except MCPXError:
            raise
        except BaseException as e:  # noqa: BLE001
            leaf = _leaf(e)
            raise MCPXError(f"{self._name}.{t.name} failed: {type(leaf).__name__}: {leaf}") from leaf
        return _convert(res, t, self._name)

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        tool = self._resolve(attr)
        def method(args: dict | None = None, /, **kwargs):
            return self.call(tool.name, args, **kwargs)
        method.__name__ = _pyname(tool.name)
        method.__doc__ = signature(tool)
        return method

    def __dir__(self):
        return [_pyname(n) for n in self.tools()] + ["call", "tools", "tool_list"]

    def __repr__(self) -> str:
        return f"<mcpx server {self._name}>"


def _parse(text: str) -> Any:
    s = text.strip()
    if s[:1] in "[{" or s in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", s or "x"):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return text


def _convert(res, tool, server_name: str) -> Any:
    from mcp.types import CallToolResult
    if not isinstance(res, CallToolResult):
        raise MCPXError(f"{server_name}.{tool.name} asked for user input; call this tool directly, not from a script")
    content = res.content or []
    texts = [c.text for c in content if getattr(c, "type", None) == "text"]
    if res.is_error:
        raise ToolError(f"{server_name}.{tool.name}: " + ("\n".join(texts) or "tool reported an error"))
    sc = res.structured_content
    if sc is not None:
        out_schema = tool.output_schema or {}
        props = out_schema.get("properties") or {}
        if isinstance(sc, dict) and list(sc) == ["result"] and (
                out_schema.get("x-fastmcp-wrap-result") or list(props) == ["result"]):
            return sc["result"]
        return sc
    others = [c for c in content if getattr(c, "type", None) != "text"]
    if not others:
        if len(texts) == 1:
            return _parse(texts[0])
        return [_parse(t) for t in texts]
    return [c.model_dump(exclude_none=True) for c in content]


# --------------------------------------------------------------------------- signatures

def _deref(schema: Any, root: dict | None, seen: int = 0) -> Any:
    while isinstance(schema, dict) and "$ref" in schema and root and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return schema
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema, seen = node, seen + 1
    return schema


def _type(schema: Any, depth: int = 0, root: dict | None = None) -> str:
    schema = _deref(schema, root)
    if not isinstance(schema, dict):
        return "any"
    if "enum" in schema:
        vals = [json.dumps(v) for v in schema["enum"][:8]]
        return "|".join(vals) + ("|..." if len(schema["enum"]) > 8 else "")
    if "const" in schema:
        return json.dumps(schema["const"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_type(s, depth, root) for s in schema[key] if s.get("type") != "null"]
            return "|".join(dict.fromkeys(parts)) or "any"
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "any")
    if t == "array":
        return f"list[{_type(schema.get('items'), depth, root)}]"
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            return "dict"
        if depth >= 1:
            names = list(props)
            return "{" + ", ".join(names[:10]) + (", ..." if len(names) > 10 else "") + "}"
        items = [f"{k}: {_type(v, depth + 1, root)}" for k, v in list(props.items())[:12]]
        return "{" + ", ".join(items) + (", ..." if len(props) > 12 else "") + "}"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}.get(t, t or "any")


def _first_sentence(desc: str | None, limit: int = 220) -> str:
    d = " ".join((desc or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", d)
    d = m.group(1) if m and len(m.group(1)) > 40 else d
    return d if len(d) <= limit else d[: limit - 3] + "..."


def signature(tool, with_desc: bool = True) -> str:
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    args = [f"{k}: {_type(v, 1, schema)}" for k, v in props.items() if k in req]
    args += [f"{k}?: {_type(v, 1, schema)}" for k, v in props.items() if k not in req]
    sig = f"{_pyname(tool.name)}({', '.join(args)})"
    if tool.output_schema:
        out = tool.output_schema
        props_out = out.get("properties") or {}
        root = out
        if list(props_out) == ["result"]:
            out = props_out["result"]
        sig += f" -> {_type(out, 0, root)}"
    if with_desc and tool.description:
        sig += f"\n    {_first_sentence(tool.description)}"
    return sig


# --------------------------------------------------------------------------- public API

def server(name: str) -> Server:
    """Connect (once per process) to a configured MCP server and return it."""
    rt = _rt()
    if name not in rt.servers:
        cfg = load_config()
        if name not in cfg:
            raise MCPXError(f"no MCP server named {name!r}; configured: {sorted(cfg) or 'none'}")
        rt.servers[name] = Server(name, cfg[name])
    return rt.servers[name]


def servers() -> dict[str, dict]:
    return load_config()


# --------------------------------------------------------------------------- CLI

def _dump(value: Any) -> str:
    """Compact JSON; lists get one item per line so capping cuts between records."""
    if isinstance(value, str):
        return value
    def one(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))
    if isinstance(value, list):
        return f"{len(value)} items\n" + "\n".join(one(v) for v in value)
    return one(value)


def main(argv: list[str]) -> int:
    import argparse
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p = argparse.ArgumentParser(prog="mcpx", description="Call MCP servers from the command line or scripts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("servers", parents=[common])
    t = sub.add_parser("tools", parents=[common])
    t.add_argument("server")
    t.add_argument("filter", nargs="?")
    t.add_argument("--no-desc", action="store_true")
    s = sub.add_parser("schema", parents=[common])
    s.add_argument("server")
    s.add_argument("tool")
    c = sub.add_parser("call", parents=[common])
    c.add_argument("server")
    c.add_argument("tool")
    c.add_argument("args", nargs="?", default="{}", help="JSON object, or - to read it from stdin")
    a = p.parse_args(argv)

    try:
        if a.cmd == "servers":
            cfg = load_config()
            if not cfg:
                print("no MCP servers configured (checked ~/.claude.json, .mcp.json, $MCPX_CONFIG)")
            for name, sc in sorted(cfg.items()):
                kind = _transport(sc)
                where = sc.get("command", "") if kind == "stdio" else re.sub(r"\?.*", "", str(sc.get("url", "")))
                print(f"{name}\t{kind}\t{sc['_scope']}\t{where}")
            return 0
        srv = server(a.server)
        if a.cmd == "tools":
            tools = srv.tool_list()
            if a.filter:
                f = a.filter.lower()
                tools = [x for x in tools if f in x.name.lower() or f in (x.description or "").lower()]
            out = "\n".join(signature(x, not a.no_desc) for x in tools)
            print(cap(f"{len(tools)} tools\n{out}", a.max_chars * 3))
        elif a.cmd == "schema":
            tool = srv._resolve(a.tool)
            print(cap(_dump({"name": tool.name, "description": tool.description,
                             "input_schema": tool.input_schema, "output_schema": tool.output_schema}),
                      a.max_chars * 3))
        elif a.cmd == "call":
            raw = sys.stdin.read() if a.args == "-" else a.args
            args = json.loads(raw or "{}")
            if not isinstance(args, dict):
                raise MCPXError("arguments must be a JSON object")
            print(cap(_dump(srv.call(a.tool, args)), a.max_chars))
        return 0
    except (MCPXError, ValueError) as e:
        print(f"mcpx: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
