---
name: k-logs
description: Tail logs for a k3s-hosted service (traefik, grafana, prometheus, freshrss, uptime-kuma, node-exporter). Thin wrapper around kubectl logs that handles namespace + label lookup so you don't have to remember them on mobile.
---

# k-logs

Tail logs from a k3s service. Saves typing `kubectl logs -n <ns> -l app=<x> --tail=N` on mobile.

## Required input

- **service** (string): one of the third-party k3s services. Common: `traefik`, `grafana`, `prometheus`, `freshrss`, `uptime-kuma`, `node-exporter`.
- **lines** (int, optional): how many lines to tail. Default 100. Cap at 500 for mobile readability.

## Steps

1. **Find the namespace + label.** Run `k get pods -A -l app=<service>` to locate the pod. If the label doesn't match (some charts use `app.kubernetes.io/name`), try `k get pods -A | grep <service>` and infer.
2. **Tail.** `k logs -n <ns> -l <label>=<service> --tail=<lines>`. If multiple pods match (e.g. DaemonSet), the output is interleaved — that's expected.
3. **Report.** Output the tail directly. If it's >500 lines or very noisy, suggest filters (e.g. `| grep ERROR`) rather than dumping.

## When to not use this

- Host-Docker apps (blog, hub, stickies, delta_neutral, tbitt) — those are in Docker Compose, not k3s. Use `docker logs <container>` instead.
- When you need to follow logs indefinitely. This skill is for a one-shot tail. For `-f` follow mode, just call `k logs ... -f` directly; don't invoke this skill.
