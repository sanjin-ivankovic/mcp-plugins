"""HTTP client, tool registration and result helpers shared by every system."""

import functools
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .config import ConfigError
from .redact import redact

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


class ApiError(Exception):
    """A backend call failed. The message is safe to show after redact()."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ToolFailure(ToolError):
    """Raised out of a tool. As a ToolError its (redacted) text reaches the client."""


class HttpClient:
    """GET-only by default. POST is limited to ``read_posts`` paths.

    Some backends expose reads over POST (Komodo ``/read``, Unraid GraphQL,
    Technitium's form API). Listing those paths explicitly keeps every
    write endpoint unreachable even if a tool passes a wrong path.
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
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.verify = verify
        self.timeout = timeout
        self.read_posts = frozenset(read_posts)

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=_clean(params))

    async def post_read(self, path: str, *, json: Any = None, data: Any = None) -> Any:
        if path not in self.read_posts:
            raise ApiError(f"{self.name}: POST {path} is not an allowlisted read endpoint")
        return await self._request("POST", path, json=json, data=data)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                verify=self.verify, timeout=self.timeout, headers=self.headers
            ) as client:
                resp = await client.request(method, self.base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(
                f"{self.name}: {method} {path} failed: {type(exc).__name__}: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise ApiError(
                f"{self.name}: {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
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


def failure_message(exc: BaseException) -> str:
    if isinstance(exc, ApiError | ConfigError):
        return redact(str(exc))
    return redact(f"{type(exc).__name__}: {exc}")


def tool(app: MCPServer, description: str) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
    """Register an async function as a read-only tool with redacted failures."""

    def deco(fn: Callable[..., Awaitable[Any]]) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - every failure must be redacted
                raise ToolFailure(failure_message(exc)) from None

        app.tool(name=fn.__name__, description=description, annotations=READ_ONLY)(wrapper)
        return fn

    return deco


def clip(items: list[Any], limit: int) -> dict[str, Any]:
    limit = max(1, min(limit, 1000))
    return {"count": len(items), "truncated": len(items) > limit, "items": items[:limit]}


def pick(obj: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: obj[k] for k in keys if k in obj and obj[k] is not None}


def pct(used: float | int | None, total: float | int | None) -> float | None:
    if not used or not total:
        return None
    return round(100.0 * float(used) / float(total), 1)
