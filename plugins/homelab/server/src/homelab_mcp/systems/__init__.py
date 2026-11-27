"""One module per backend. Each exposes register(app, cfg) and, where useful, summary(cfg)."""

from . import (
    alertmanager,
    drift,
    gitlab,
    komodo,
    ntfy,
    pbs,
    pihole,
    pve,
    semaphore,
    technitium,
    traefik,
    unraid,
    vault,
)

MODULES = (
    pve,
    pbs,
    unraid,
    technitium,
    pihole,
    komodo,
    semaphore,
    traefik,
    alertmanager,
    ntfy,
    vault,
    gitlab,
    drift,
)
