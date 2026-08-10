#!/bin/bash
# Inject a DOCKER-USER rule to block LAN access to the blog
# These survive Docker restarts because DOCKER-USER chain persists

# Blog (172.18.0.2:3099 on br-f8219785fa8f)
if ! iptables -C DOCKER-USER -i enp3s0f0 -d 172.18.0.2 -p tcp --dport 3099 -j DROP 2>/dev/null; then
    iptables -I DOCKER-USER 1 -i enp3s0f0 -d 172.18.0.2 -p tcp --dport 3099 -j DROP
fi
