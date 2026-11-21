---
type: llm
---

PASS if the reply says the domain is blocked by a gravity blocklist (lists.test/ads-hosts.txt) on both Pi-hole nodes, and that the lasting fix is an allowlist entry made through the proxmox-infra repository (Pi-hole configuration as code), not by clicking in the Pi-hole web UI.
FAIL if it only recommends allowing the domain in the Pi-hole web UI, or does not name the blocking list.
