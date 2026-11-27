"""HTTP client, tool registration and result helpers shared by every server."""

import argparse
import asyncio
import functools
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Hashable, Iterable
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .config import Config, ConfigError
from .redact import redact

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
# Only the opengist, docmost and silverbullet servers register WRITE or
# DESTRUCTIVE tools; the homelab server stays READ_ONLY
# (test_every_tool_is_annotated_read_only).
# Adds data and never changes what exists: a new gist or page, an append.
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
# Can replace or remove what exists: an overwrite, a replaced body, a removed label.
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
# Claude Code saves a text result over 50,000 characters to a file unless the
# tool raises the threshold through this _meta key (hard ceiling 500,000).
MAX_RESULT_CHARS = "anthropic/maxResultSizeChars"
# Claude Code keeps a tool marked with this _meta key out of tool search and
# loads it upfront in every session; reserve it for the documented entry point.
ALWAYS_LOAD = "anthropic/alwaysLoad"


# Gateway errors a GET is retried after, and the pause before each retry.
RETRY_STATUS = frozenset({502, 503, 504})
RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5)


class ApiError(Exception):
    """A backend call failed. The message is safe to show after redact()."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InputError(Exception):
    """A tool argument is wrong (unknown node, empty query, no such guest)."""


class ToolFailure(ToolError):
    """Raised out of a tool. As a ToolError its (redacted) text reaches the client."""


# One connection pool per TLS mode for the life of the process. A client is
# built per request on the shared transport, so cookies and headers never
# carry over between backends; only the connections are reused. A pool binds
# to the event loop that first uses it: call close_pools() before a new loop.
_TRANSPORTS: dict[bool, httpx.AsyncHTTPTransport] = {}


def transport(verify: bool) -> httpx.AsyncHTTPTransport:
    if verify not in _TRANSPORTS:
        # retries covers only failures to connect, where nothing was sent, so it
        # is safe for writes too.
        _TRANSPORTS[verify] = httpx.AsyncHTTPTransport(verify=verify, retries=2)
    return _TRANSPORTS[verify]


def http_client(
    *, verify: bool = True, timeout: float = 20.0, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    """A client on the shared pool. Never close it: that closes the pool."""
    return httpx.AsyncClient(transport=transport(verify), timeout=timeout, headers=headers)


async def close_pools() -> None:
    for t in _TRANSPORTS.values():
        await t.aclose()
    _TRANSPORTS.clear()


class HttpClient:
    """GET-only by default. POST is limited to ``read_posts`` and ``write_posts`` paths.

    Some backends expose reads over POST (Komodo ``/read``, Unraid GraphQL,
    Technitium's form API). Listing those paths explicitly keeps every
    write endpoint unreachable even if a tool passes a wrong path.
    ``write_posts`` is empty for every homelab system; only the opengist and
    docmost servers set it. PUT is limited to paths under ``write_put_prefixes``,
    which only the silverbullet server sets.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        verify: bool = True,
        timeout: float = 20.0,
        read_posts: Iterable[str] = (),
        write_posts: Iterable[str] = (),
        write_put_prefixes: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.verify = verify
        self.timeout = timeout
        self.read_posts = frozenset(read_posts)
        self.write_posts = frozenset(write_posts)
        self.write_put_prefixes = tuple(write_put_prefixes)

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=_clean(params))

    async def get_all(
        self, path: str, *, per_page: int = 100, max_pages: int = 10, **params: Any
    ) -> list[Any]:
        """Every item of a ``page``/``per_page`` list endpoint, up to ``max_pages`` pages."""
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            batch: list[Any] = await self.get(path, page=page, per_page=per_page, **params) or []
            items += batch
            if len(batch) < per_page:
                break
        return items

    async def get_text(self, path: str, **params: Any) -> str:
        """GET a plain-text body (a job log, for example)."""
        return await self._request("GET", path, params=_clean(params), text=True)

    async def post_read(self, path: str, *, json: Any = None, data: Any = None) -> Any:
        if path not in self.read_posts:
            raise ApiError(f"{self.name}: POST {path} is not an allowlisted read endpoint")
        return await self._request("POST", path, json=json, data=data)

    async def post_write(self, path: str, *, json: Any) -> Any:
        if path not in self.write_posts:
            raise ApiError(f"{self.name}: POST {path} is not an allowlisted write endpoint")
        return await self._request("POST", path, json=json)

    async def get_response(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET returning the response itself, for callers that need headers (ETag)."""
        return await self._send("GET", path, **kwargs)

    async def put_write(
        self, path: str, *, content: bytes, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if not self.write_put_prefixes or not path.startswith(self.write_put_prefixes):
            raise ApiError(f"{self.name}: PUT {path} is not an allowlisted write endpoint")
        return await self._send("PUT", path, content=content, headers=headers)

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = http_client(verify=self.verify, timeout=self.timeout, headers=self.headers)
        # A GET is retried after a gateway error or a dropped connection; a POST
        # or PUT never is, because the backend may already have applied it.
        delays = RETRY_DELAYS if method == "GET" else ()
        attempt = 0
        while True:
            try:
                resp = await client.request(method, self.base_url + path, **kwargs)
            except httpx.RemoteProtocolError as exc:
                if attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
                    attempt += 1
                    continue
                raise ApiError(
                    f"{self.name}: {method} {path} failed: {type(exc).__name__}: {exc}"
                ) from None
            except httpx.HTTPError as exc:
                raise ApiError(
                    f"{self.name}: {method} {path} failed: {type(exc).__name__}: {exc}"
                ) from None
            if resp.status_code in RETRY_STATUS and attempt < len(delays):
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            break
        if resp.status_code >= 400:
            raise ApiError(
                f"{self.name}: {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
        return resp

    async def _request(self, method: str, path: str, *, text: bool = False, **kwargs: Any) -> Any:
        resp = await self._send(method, path, **kwargs)
        if text:
            return resp.text
        if not resp.content.strip():
            return {}
        try:
            return resp.json()
        except ValueError:
            raise ApiError(f"{self.name}: {method} {path} returned non-JSON") from None


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        out[key] = str(value).lower() if isinstance(value, bool) else value
    return out


def client_from(
    cfg: Config,
    name: str,
    *,
    path: str = "",
    headers: dict[str, str] | None = None,
    read_posts: Iterable[str] = (),
    write_posts: Iterable[str] = (),
    write_put_prefixes: Iterable[str] = (),
    url: str | None = None,
) -> HttpClient:
    """An HttpClient for section ``[name]``: its ``url`` (or ``url``) plus ``path``,
    honouring ``verify_tls`` and ``timeout`` from the section."""
    sec = cfg.section(name)
    base = url or cfg.require(sec, "url", name)
    return HttpClient(
        name,
        base.rstrip("/") + path,
        headers=headers,
        verify=bool(sec.get("verify_tls", True)),
        timeout=float(sec.get("timeout", 20.0)),
        read_posts=read_posts,
        write_posts=write_posts,
        write_put_prefixes=write_put_prefixes,
    )


class Session:
    """A login token cached in ``cache[key]``, renewed once when a call answers 401.

    One lock per key, so concurrent calls on a cold cache log in once.
    """

    def __init__(
        self, cache: dict[Hashable, Any], key: Hashable, login: Callable[[], Awaitable[str]]
    ) -> None:
        self.cache = cache
        self.key = key
        self.login = login
        self.lock: asyncio.Lock = cache.setdefault(("lock", key), asyncio.Lock())

    async def token(self) -> str:
        async with self.lock:
            if self.key not in self.cache:
                self.cache[self.key] = await self.login()
            return self.cache[self.key]

    async def call[R](self, fn: Callable[[str], Awaitable[R]]) -> R:
        token = await self.token()
        try:
            return await fn(token)
        except ApiError as exc:
            if exc.status != 401:
                raise
        async with self.lock:
            # Another call may have renewed it meanwhile; drop only the stale one.
            if self.cache.get(self.key) == token:
                del self.cache[self.key]
        return await fn(await self.token())


def failure_message(exc: BaseException) -> str:
    if isinstance(exc, ApiError | ConfigError | InputError):
        return redact(str(exc))
    return redact(f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def _close_pools_on_exit(_: MCPServer[None]) -> AsyncGenerator[None]:
    try:
        yield
    finally:
        await close_pools()


def make_app(name: str, instructions: str) -> MCPServer[None]:
    """A server named ``name`` at the package version that closes the HTTP pools on exit."""
    return MCPServer(
        name, version=__version__, instructions=instructions, lifespan=_close_pools_on_exit
    )


def tool(
    app: MCPServer[Any],
    description: str,
    *,
    title: str,
    annotations: ToolAnnotations = READ_ONLY,
    meta: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
    """Register an async function as a tool (read-only unless told) with redacted failures.

    The name is the function name. ``title`` is the human-readable name clients
    show; describe every parameter with ``Annotated[T, Field(description=...)]``.
    """

    def deco(fn: Callable[..., Awaitable[Any]]) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - every failure must be redacted
                raise ToolFailure(failure_message(exc)) from None

        app.tool(
            name=fn.__name__,
            title=title,
            description=description,
            annotations=annotations,
            meta=meta,
        )(wrapper)
        return fn

    return deco


async def gather_map[T, R](
    items: Iterable[T], fn: Callable[[T], Awaitable[R]], limit: int = 6
) -> list[R]:
    """``fn`` over ``items`` concurrently, at most ``limit`` at a time, results in order."""
    sem = asyncio.Semaphore(limit)

    async def one(item: T) -> R:
        async with sem:
            return await fn(item)

    return list(await asyncio.gather(*(one(i) for i in items)))


def select[T](items: list[T], name: str | None, key: Callable[[T], str], what: str) -> list[T]:
    """All items, or the one whose key is ``name``; an unknown name lists the known ones."""
    if name is None:
        return items
    match = [i for i in items if key(i) == name]
    if not match:
        raise InputError(f"unknown {what} '{name}'; known: {[key(i) for i in items]}")
    return match


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def log_tail(text: str, max_chars: int = 4000) -> str:
    """Redacted, colour-free end of a command's output, at most ``max_chars``."""
    text = redact(strip_ansi(text)).strip()
    return text if len(text) <= max_chars else "…" + text[-max_chars:]


def tail_lines(lines: list[str], n: int, cap: int = 2000) -> dict[str, Any]:
    """The last ``n`` (at most ``cap``) lines, redacted, with the total and a truncated flag."""
    keep = clamp(n, 1, cap)
    return {
        "lines_total": len(lines),
        "truncated": len(lines) > keep,
        "output": redact("\n".join(lines[-keep:])),
    }


class Listing(TypedDict):
    """At most ``limit`` items, how many there were, and whether some were cut."""

    count: int
    truncated: bool
    items: list[Any]


def clip(items: list[Any], limit: int) -> Listing:
    limit = clamp(limit, 1, 1000)
    return {"count": len(items), "truncated": len(items) > limit, "items": items[:limit]}


def obj(value: Any) -> dict[str, Any]:
    """``value`` if the backend sent a JSON object, else ``{}``."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def arr(value: Any) -> list[Any]:
    """``value`` if the backend sent a JSON array, else ``[]``."""
    return cast(list[Any], value) if isinstance(value, list) else []


def pick(obj: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: obj[k] for k in keys if k in obj and obj[k] is not None}


def pct(used: float | int | None, total: float | int | None) -> float | None:
    if not used or not total:
        return None
    return round(100.0 * float(used) / float(total), 1)


def serve(
    prog: str, description: str | None, build: Callable[[Config], MCPServer], port: int
) -> None:
    """The command line every server shares: --config, stdio by default, --http."""
    from . import config as config_mod

    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--config",
        help="config.toml path (default: $HOMELAB_MCP_CONFIG or ~/.config/homelab-mcp/config.toml)",
    )
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=port)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="report an unreadable config or unknown keys and exit (1 when there are any)",
    )
    args = parser.parse_args()
    if args.check_config:
        cfg = config_mod.load(args.config)
        problems = [cfg.load_error] if cfg.load_error else cfg.warnings
        for problem in problems:
            print(f"{cfg.path}: {problem}")
        raise SystemExit(1 if problems else 0)
    # httpx logs every request URL at INFO; keep stderr for real problems.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build(config_mod.load(args.config))
    if args.http:
        app.run("streamable-http", host=args.host, port=args.port)
    else:
        app.run("stdio")
