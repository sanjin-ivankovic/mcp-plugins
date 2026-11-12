"""One module per backend. Each exposes register(app, cfg) and, where useful, summary(cfg)."""

from . import drift, komodo, pbs, pihole, pve, semaphore, technitium, unraid

MODULES = (pve, pbs, unraid, technitium, pihole, komodo, semaphore, drift)
