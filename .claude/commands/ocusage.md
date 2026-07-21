Fetch and display aggregate OpenCode Go billing usage from the opencode-go-proxy.

Run this command and display the output exactly as returned:

```bash
curl -s http://localhost:8082/usage
```

If the curl fails, say "Proxy unreachable — check systemctl --user status opencode-go-proxy" and stop.
