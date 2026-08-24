# homelab

Automation for my homelab

GitOps automation for a single-server Proxmox VE homelab, in four stages, each
of which can rebuild the one above it:

0. **[Network](stage0-network/)** — VLANs, inter-VLAN routing and ACLs, DHCP/resolver, the router↔switch boundary. Ansible, run by hand from a workstation, never reconciled
1. **Bootstrap** — Proxmox host config, internal DNS, secrets root, job-runner UI
2. **Cluster lifecycle** — Kubernetes cluster creation, upgrades and maintenance
3. **Platform & apps** — ingress, certificates, storage, monitoring, PostgreSQL, applications
