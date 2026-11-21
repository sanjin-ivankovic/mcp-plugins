---
type: llm
---

PASS if the reply says Traefik's backend for app.test (service app-svc, server 10.42.1.17:8080, Kubernetes namespace app) is DOWN, so the problem is the app's pod or service rather than the route.
FAIL if it blames DNS, TLS or the router rule.
