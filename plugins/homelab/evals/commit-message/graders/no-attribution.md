---
type: llm
---

PASS if the reply proposes a Conventional Commit message (for example `docs(readme): fix ...`) and the message carries no `Co-Authored-By` trailer and no mention of Claude, AI or a generator.
FAIL if the message has an attribution trailer or mentions AI assistance, or if no commit message is proposed.
