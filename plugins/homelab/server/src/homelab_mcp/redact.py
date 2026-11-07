"""Scrub secrets from any text that leaves the server (errors, subprocess output)."""

import re

_SECRETS: set[str] = set()

_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+(\s+\S+)?)"),
    re.compile(r"(?i)\b(PVEAPIToken|PBSAPIToken)=\S+"),
    re.compile(
        r"(?i)\b((?:api[_-]?)?(?:token|password|passwd|secret|apikey|api_key)\s*[=:]\s*)(\S+)"
    ),
    re.compile(r"(?i)([?&](?:token|apiKey|api_key|password)=)[^&\s]+"),
]


def register(value: str | None) -> None:
    """Remember a resolved secret so every later redact() masks it verbatim."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pattern.sub(lambda m: m.group(0).split("=")[0] + "=***", text)
    return text
