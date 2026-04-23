---
name: Do not run aa-remove-unknown
description: Running aa-remove-unknown on this host removes snap Docker's AppArmor profiles, breaking Docker entirely.
type: feedback
originSessionId: b46dad96-f1a5-4d76-a140-f5b64e6fd75d
---
Never run `sudo aa-remove-unknown` on this host.

**Why:** Snap-installed Docker depends on AppArmor profiles (e.g. `snap.docker.dockerd`) that `aa-remove-unknown` classifies as "unknown" and deletes. This causes Docker to crashloop with "missing profile snap.docker.dockerd" and all containers go down. Recovery requires `sudo systemctl restart snapd.apparmor` to reload the profiles, then restarting Docker — but any running containers will have exited by then.

**How to apply:** If Docker containers can't be stopped/killed due to AppArmor "permission denied" errors, fix by restarting `snapd.apparmor` and then Docker (`sudo systemctl restart snapd.apparmor && sudo snap start docker.dockerd`) — not by clearing AppArmor profiles.
