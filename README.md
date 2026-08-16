# homelab
Automation for my homelab

GitOps automation for a single-server Proxmox VE homelab, in three stages:

1. **Bootstrap** — Proxmox host config, DHCP/DNS, secrets root, job-runner UI, management cluster
2. **Cluster lifecycle** — autoscaled Kubernetes cluster creation, upgrades and maintenance
3. **Platform & apps** — ingress, certificates, storage, monitoring, PostgreSQL, applications

See [docs/PLAN.md](docs/PLAN.md) for the full plan and the open decision register.
