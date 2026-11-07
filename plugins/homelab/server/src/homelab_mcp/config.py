"""Load ~/.config/homelab-mcp/config.toml and resolve secrets.

Secrets are never written inline. Every secret key ``<name>`` is given as
``<name>_file`` (path, relative paths resolve against the config dir) or
``<name>_command`` (argv list, run without a shell, stdout is the value).
A missing section disables that system's tools; it never stops the server.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import redact

DEFAULT_PATH = Path("~/.config/homelab-mcp/config.toml")


class ConfigError(Exception):
    """A system is not configured, or its secret cannot be resolved."""


@dataclass
class Config:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    load_error: str | None = None

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
        return sec

    def resolve_path(self, value: str) -> Path:
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else self.dir / p

    def secret(self, sec: dict[str, Any], key: str, where: str) -> str:
        """Resolve ``key`` from ``key_file`` or ``key_command`` in ``sec``."""
        if f"{key}_file" in sec:
            path = self.resolve_path(str(sec[f"{key}_file"]))
            try:
                value = path.read_text().strip()
            except OSError as exc:
                raise ConfigError(
                    f"{where}: cannot read {key}_file {path}: {exc.strerror}"
                ) from None
        elif f"{key}_command" in sec:
            argv = sec[f"{key}_command"]
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
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
        return value


def load(path: str | Path | None = None) -> Config:
    raw = path or os.environ.get("HOMELAB_MCP_CONFIG") or str(DEFAULT_PATH)
    p = Path(os.path.expanduser(str(raw)))
    if not p.exists():
        return Config(p, load_error=f"config file {p} not found (see docs/SETUP.md)")
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(p, load_error=f"cannot parse {p}: {exc}")
    return Config(p, data)
