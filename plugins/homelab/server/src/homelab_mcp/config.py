"""Load ~/.config/homelab-mcp/config.toml and resolve secrets.

Secrets are never written inline. Every secret key ``<name>`` is given as
``<name>_file`` (path, relative paths resolve against the config dir) or
``<name>_command`` (argv list, run without a shell, stdout is the value).
A missing section disables that system's tools; it never stops the server.
A resolved secret is cached for the life of the process, so a ``_command``
runs once; after rotating a secret, reconnect the server (``/mcp``).
"""

import difflib
import os
import subprocess
import tomllib
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from . import redact

DEFAULT_PATH = Path("~/.config/homelab-mcp/config.toml")


class ConfigError(Exception):
    """A system is not configured, or its secret cannot be resolved."""


@dataclass
class Config:
    path: Path
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    load_error: str | None = None
    # Keys and sections no server reads: a typo here is otherwise silently ignored.
    warnings: list[str] = field(default_factory=list[str])
    # Per-process state the servers keep between calls: login sessions, slug maps.
    cache: dict[Hashable, Any] = field(default_factory=dict[Hashable, Any])
    _secrets: dict[tuple[str, str], str] = field(
        default_factory=dict[tuple[str, str], str], repr=False
    )

    @property
    def dir(self) -> Path:
        return self.path.parent

    def has(self, name: str) -> bool:
        return isinstance(self.data.get(name), dict)

    def section(self, name: str) -> dict[str, Any]:
        if self.load_error:
            raise ConfigError(self.load_error)
        sec = self.data.get(name)
        if not isinstance(sec, dict):
            raise ConfigError(f"[{name}] is not configured in {self.path}")
        return cast(dict[str, Any], sec)

    def require(self, sec: dict[str, Any], key: str, where: str) -> str:
        """A required plain (non-secret) value of a section."""
        value = sec.get(key)
        if value is None or value == "":
            raise ConfigError(f"[{where}] is missing '{key}' in {self.path}")
        return str(value)

    def resolve_path(self, value: str) -> Path:
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else self.dir / p

    def secret(self, sec: dict[str, Any], key: str, where: str) -> str:
        """Resolve ``key`` from ``key_file`` or ``key_command`` in ``sec``, once."""
        if f"{key}_file" in sec:
            spec = ("file", str(sec[f"{key}_file"]))
        elif f"{key}_command" in sec:
            spec = ("command", repr(sec[f"{key}_command"]))
        else:
            spec = ("", "")
        if spec in self._secrets:
            return self._secrets[spec]
        if f"{key}_file" in sec:
            path = self.resolve_path(str(sec[f"{key}_file"]))
            try:
                value = path.read_text().strip()
            except OSError as exc:
                raise ConfigError(
                    f"{where}: cannot read {key}_file {path}: {exc.strerror}"
                ) from None
        elif f"{key}_command" in sec:
            raw: object = sec[f"{key}_command"]
            items = cast(list[object], raw) if isinstance(raw, list) else []
            argv = [a for a in items if isinstance(a, str)]
            if not argv or len(argv) != len(items):
                raise ConfigError(f"{where}: {key}_command must be a list of strings")
            try:
                out = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConfigError(f"{where}: {key}_command failed: {type(exc).__name__}") from None
            value = out.stdout.strip()
        elif key in sec:
            raise ConfigError(
                f"{where}: inline '{key}' is refused; use {key}_file or {key}_command"
            )
        else:
            raise ConfigError(f"{where}: missing {key}_file or {key}_command")
        if not value:
            raise ConfigError(f"{where}: {key} resolved to an empty value")
        redact.register(value)
        self._secrets[spec] = value
        return value


# Keys each section accepts, as (plain keys, secret keys). A secret key ``k``
# is written ``k_file`` or ``k_command``, never inline. config.example.toml
# documents every section (tests/test_config.py keeps the two in step).
_HTTP = ("url", "verify_tls", "timeout")
SECTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "repos": (("argo_apps", "proxmox_infra"), ()),
    "pve": ((*_HTTP, "token_id"), ("token_secret",)),
    "pbs": ((*_HTTP, "token_id"), ("token_secret",)),
    "unraid": (_HTTP, ("api_key",)),
    "technitium": (("nodes", "verify_tls"), ("token",)),
    "pihole": (("nodes", "verify_tls"), ()),
    "komodo": (_HTTP, ("key", "secret")),
    "semaphore": (_HTTP, ("token",)),
    "argocd": (_HTTP, ("token",)),
    "kubernetes": (("kubeconfig",), ()),
    "vault": ((*_HTTP, "role_id", "mount", "eso_prefix"), ("secret_id",)),
    "traefik": (_HTTP, ()),
    "alertmanager": (_HTTP, ()),
    "ntfy": (_HTTP, ("token",)),
    "gitlab": (_HTTP, ("token",)),
    "searxng": ((*_HTTP, "language", "fetch_max_bytes"), ()),
    "opengist": (_HTTP, ("token",)),
    "silverbullet": (_HTTP, ("token",)),
    "docmost": (("url", "verify_tls", "email", "space"), ("password",)),
}
# Keys of each entry in a section's ``nodes`` list.
NODES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "technitium": (("name", "url", "dhcp", "verify_tls"), ("token",)),
    "pihole": (("name", "url", "verify_tls"), ("credentials",)),
}


def _unknown(
    where: str, table: dict[str, Any], plain: tuple[str, ...], secrets: tuple[str, ...]
) -> list[str]:
    allowed = sorted({*plain, *(f"{s}_{kind}" for s in secrets for kind in ("file", "command"))})
    out: list[str] = []
    for key in table:
        if key in allowed:
            continue
        hint = difflib.get_close_matches(key, allowed, n=1)
        out.append(
            f"{where}: unknown key '{key}'" + (f", did you mean '{hint[0]}'?" if hint else "")
        )
    return out


def check(data: dict[str, Any]) -> list[str]:
    """Sections and keys no server reads, each with the closest known name."""
    problems: list[str] = []
    for name, sec in data.items():
        if name not in SECTIONS:
            hint = difflib.get_close_matches(name, sorted(SECTIONS), n=1)
            problems.append(
                f"unknown section [{name}]" + (f", did you mean [{hint[0]}]?" if hint else "")
            )
            continue
        if not isinstance(sec, dict):
            problems.append(f"[{name}] is not a table")
            continue
        table = cast(dict[str, Any], sec)
        problems += _unknown(f"[{name}]", table, *SECTIONS[name])
        if name in NODES:
            nodes: object = table.get("nodes") or []
            for i, node in enumerate(cast(list[object], nodes) if isinstance(nodes, list) else []):
                if isinstance(node, dict):
                    problems += _unknown(
                        f"[{name}] nodes[{i}]", cast(dict[str, Any], node), *NODES[name]
                    )
    return problems


def load(path: str | Path | None = None) -> Config:
    raw = path or os.environ.get("HOMELAB_MCP_CONFIG") or str(DEFAULT_PATH)
    p = Path(os.path.expanduser(str(raw)))
    if not p.exists():
        return Config(p, load_error=f"config file {p} not found (see docs/SETUP.md)")
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(p, load_error=f"cannot parse {p}: {exc}")
    return Config(p, data, warnings=check(data))
