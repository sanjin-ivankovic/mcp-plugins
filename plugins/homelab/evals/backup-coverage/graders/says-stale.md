---
type: llm
---

PASS if the reply says scratch-db (VMID 140) has backups but its newest one is stale, about six days old against a 36-hour limit.
FAIL if it says scratch-db is backed up and current, or that it has no backup at all.
