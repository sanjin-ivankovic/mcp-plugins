---
type: llm
---

PASS if the reply reports that PBS (Proxmox Backup Server) could not be reached or answered with an error (a connect timeout), as a problem or something to check again, and does not claim that everything is healthy.
FAIL if it does not mention the PBS error, or says the whole homelab is healthy.
