---
max_turns: 12
allowed_tools: [Skill]
tags: [routing, writes]
---

Put this command somewhere I can copy it on my work laptop: kubectl get pods -A --field-selector=status.phase!=Running
