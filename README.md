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
- FreshRSS
- Uptime Kuma

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
        - Can also use namespace and query (`-l`)
            - `kubectl logs -n [namespace] -l app=[appname]`
    - Restart pod
        - Kubernetes will automatically recreate deleted pods
        - `kubectl delete [pod_name]`

Cluster configurations are organized using a service-based directory structure, 
where each service has its own dedicated directory containing granular YAML manifests.

```
k3s/
├── service1/
│   ├── service1-deployment.yaml
│   ├── service1-ingress.yaml
│   └── service1-service.yaml
├── service2/
│   ├── service2-deployment.yaml
│   ├── service2-configmap.yaml
│   └── service2-ingress.yaml
...
```

## TODO
- Applications
    - Media server
    - Cloud storage
    - Home surveillance
- Infrastructure
    - NAS to support cloud storage and media server

