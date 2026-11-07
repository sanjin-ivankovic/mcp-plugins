from pathlib import Path

import pytest

from homelab_mcp import config


@pytest.fixture
def anyio_backend():
    return "asyncio"


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

[argocd]
url = "https://argo.test"
token_file = "secrets/argocd"
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
    "argocd": "argocd-token-value",
}


@pytest.fixture
def full_cfg(make_cfg):
    return make_cfg(FULL, SECRETS)
