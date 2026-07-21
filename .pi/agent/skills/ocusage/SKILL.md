---
name: ocusage
description: Show real-time aggregate OpenCode Go billing usage across all proxy-managed accounts
---

# ocusage

Fetch and display aggregate OpenCode Go billing usage from the opencode-go-proxy. This shows real billing data scraped from opencode.ai dashboards — more accurate than the built-in `/usage` for total spend because it includes non-omp traffic (Open WebUI, etc.).

## Steps

1. Call the proxy's usage endpoint:

```bash
curl -s http://localhost:8082/usage
```

2. Display the output exactly as returned. No need to reformat or summarize — the proxy already returns a clean text report.

3. If the curl fails, report the error and suggest checking: `systemctl --user status opencode-go-proxy`
