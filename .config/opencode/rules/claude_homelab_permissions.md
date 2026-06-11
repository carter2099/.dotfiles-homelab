---
name: claude-homelab agent has wide-open permissions
description: The persistent claude-homelab systemd service (driven from mobile /remote) is intentionally unrestricted. Threat model only worries about the mobile app as inbound; no email/SMS ingress.
type: feedback
originSessionId: 59cee07f-6a07-48f6-88c9-b642bbd2b665
---
For the `claude-homelab.service` persistent agent (systemd user unit running `claude remote-control`, driven from the Claude mobile app), the user explicitly wants wide-open permissions — "give this thing every permission possible."

**Why:** The only inbound path to this agent is the Claude mobile app (authenticated to his claude.ai Pro account). No email, SMS, Telegram, webhooks, or other channels that could carry prompt-injection payloads. He's accepted that the mobile app's trust boundary is the whole security model, so per-tool allowlists are friction without risk reduction.

**How to apply:**
- For `.claude/settings.local.json` on this homelab: prefer broad wildcards (`Bash(*)`, `WebFetch(*)`) over narrow per-command entries when working on this project.
- Passwordless sudo: narrow (reboot/poweroff only) is still preferred — that's blast-radius control, not prompt-injection control. Don't broaden unless asked.
- If he ever adds Channels (Telegram/Discord/webhook) or similar inbound integrations later, this preference should be **revisited** — the threat model changes the moment untrusted text can reach the agent.
