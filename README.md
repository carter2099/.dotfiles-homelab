# System Notes

## General
- Device: 2017 14" Macbook Pro
    - Intel i5-7360U (4) @ 3.600GHz
    - 8GB RAM
- OS: Ubuntu Server
- Shell: zsh
- Hostname: mbp-ubuntu-server

A single node Kubernetes cluster (k3s) hosting
- Grafana and Prometheus
- Nextcloud

## Kubernetes
- Use `kubectl` to control cluster
    - Get nodes
        - `kubectl get nodes`
    - Get pods
        - `kubectl get pods`
    - Get services
        - `kubectl get svc`
    - Get logs for pod
        - `kubectl logs [pod_name]`
    - Restart pod
        - Kubernetes will automatically recreate deleted pods
        - `kubectl delete [pod_name]`
- Use `helm` package manager
    - Install and uninstall
        - `helm install`
        - `helm uninstall`

## TODO
- System
    - Move off of `helm`
- Applications
    - FreshRss
- Infrastructure
    - NAS to support Nextcloud

