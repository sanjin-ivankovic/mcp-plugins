import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from homelab_mcp import common, config, redact


@pytest.fixture(autouse=True)
def _structured_matches_text(monkeypatch):
    """Every successful tool call in the suite: structuredContent, which the
    client passes to the model, must carry every field of the text block. A
    typed result whose declared shape misses a field fails here instead of
    dropping that field silently."""
    original = Client.call_tool

    async def checked(self: Client, *args: Any, **kwargs: Any):
        result = await original(self, *args, **kwargs)
        if not result.is_error and result.structured_content is not None:
            texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
            if len(texts) == 1:
                assert result.structured_content == json.loads(texts[0]), (
                    f"{args[0] if args else kwargs.get('name')}: structured content differs "
                    "from the text block (a field missing from the result type?)"
                )
        return result

    monkeypatch.setattr(Client, "call_tool", checked)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def _isolate(anyio_backend, monkeypatch):
    """Every test starts with fresh pools (they bind to the test's event loop)
    and no secrets remembered from earlier tests. Retries keep their count but
    do not wait."""
    monkeypatch.setattr(common, "RETRY_DELAYS", (0.0, 0.0))
    yield
    await common.close_pools()
    redact.reset()


@pytest.fixture
def make_cfg(tmp_path: Path):
    """Write config.toml plus secret files into tmp_path and load it."""

    def _make(toml: str, secrets: dict[str, str] | None = None) -> config.Config:
        sec_dir = tmp_path / "secrets"
        sec_dir.mkdir(exist_ok=True)
        for name, value in (secrets or {}).items():
            (sec_dir / name).write_text(value + "\n")
        path = tmp_path / "config.toml"
        path.write_text(toml)
        return config.load(path)

    return _make


FULL = """
[pve]
url = "https://pve.test:8006"
token_id = "mcp@pve!readonly"
token_secret_file = "secrets/pve"

[pbs]
url = "https://pbs.test:8007"
token_id = "mcp@pbs!readonly"
token_secret_file = "secrets/pbs"

[unraid]
url = "http://tower.test/graphql"
api_key_file = "secrets/unraid"

[technitium]
token_file = "secrets/technitium"
nodes = [{ name = "ns1", url = "http://ns1.test:5380", dhcp = true },
         { name = "ns2", url = "http://ns2.test:5380" }]

[pihole]
nodes = [{ name = "pihole-1", url = "http://pihole-1.test", credentials_file = "secrets/pihole" },
         { name = "pihole-2", url = "http://pihole-2.test", credentials_file = "secrets/pihole" }]

[komodo]
url = "http://komodo.test:9120"
key_file = "secrets/komodo-key"
secret_file = "secrets/komodo-secret"

[semaphore]
url = "http://semaphore.test:3000"
token_file = "secrets/semaphore"

[traefik]
url = "https://traefik.test"

[alertmanager]
url = "https://alertmanager.test"

[ntfy]
url = "https://ntfy.test"
token_file = "secrets/ntfy"

[vault]
url = "https://vault.test:8200"
role_id = "role-id-uuid"
secret_id_file = "secrets/vault-secret-id"

[argocd]
url = "https://argo.test"
token_file = "secrets/argocd"

[gitlab]
url = "https://git.test"
token_file = "secrets/gitlab"

[opengist]
url = "https://gist.test"
token_file = "secrets/opengist"

[searxng]
url = "https://search.test"

[silverbullet]
url = "https://notes.test/scratch"
token_file = "secrets/silverbullet"

[docmost]
url = "https://docmost.test"
email = "claude@docmost.test"
password_file = "secrets/docmost"
"""

SECRETS = {
    "pve": "pve-secret-uuid",
    "pbs": "pbs-secret-uuid",
    "unraid": "unraid-key-value",
    "technitium": "technitium-token-value",
    "pihole": "pihole-1 pihole1-app-password\npihole-2 pihole2-app-password",
    "komodo-key": "komodo-key-value",
    "komodo-secret": "komodo-secret-value",
    "semaphore": "semaphore-token-value",
    "ntfy": "tk_ntfy-token-value",
    "argocd": "argocd-token-value",
    "gitlab": "gitlab-token-value",
    "opengist": "og_opengist-token-value",
    "silverbullet": "sb_silverbullet-token-value",
    "docmost": "docmost-password-value",
    "vault-secret-id": "vault-secret-id-value",
}


@pytest.fixture
def full_cfg(make_cfg):
    return make_cfg(FULL, SECRETS)
