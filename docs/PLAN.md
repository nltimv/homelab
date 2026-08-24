# Homelab GitOps Automation — Plan

> **Status: all decisions made** (§9). Phase 0 can begin.
> Last updated: 2026-08-20.

---

## 1. Goal

Automate a single-server homelab, from bare Proxmox VE to running workloads, in four
stages that each have a clear boundary and a clear "break glass" story.

| Stage | Owns | Driven by | Lives where |
|---|---|---|---|
| **0 — Network** | VLANs, inter-VLAN routing and ACLs, DHCP/resolver, the router↔switch boundary | Ansible, run from a workstation, **manually triggered** | OPNsense + HPE 5130 |
| **1 — Bootstrap** | Proxmox host config, internal DNS, secrets root, job-runner UI | Ansible, run from a workstation | Proxmox host + 3 LXCs |
| **2 — Cluster lifecycle** | Creating/upgrading/repairing the workload Kubernetes cluster | OpenTofu + talhelper + `talosctl`, triggered from the `runner` LXC | `runner` LXC |
| **3 — Platform & apps** | Ingress, certs, storage classes, monitoring, Postgres, apps | GitOps (Flux on the workload cluster) | Workload cluster |

### Design principles

1. **Each stage can rebuild the one above it.** Stage 1 survives the total loss of the
   Kubernetes cluster and can recreate it; stage 0 survives the loss of everything else.
   This is the single most important property and it drives the placement decisions below.
2. **Only stage 3 is continuously reconciled.** Stages 0–2 are the chicken-and-egg layers:
   fully described in Git, but *applied when triggered*, not by a controller watching a branch.
   Stage 0 goes further and is **never** reconciled automatically, because a controller that
   manages the network it depends on cannot roll back its own mistakes (§4). Stage 2 is
   triggered from the `runner` LXC (§6), which is deliberately outside the cluster it builds.
3. **Declarative over imperative wherever a controller exists.** Upgrades should be a
   version bump in a YAML file, not a runbook.
4. **Assume the host dies.** Nothing here provides hardware fault tolerance, so off-box
   backups are the real disaster-recovery story, not replication (§2.2, §7.7).

---

## 2. Constraints — read this before choosing anything

### 2.1 The hardware

| | |
|---|---|
| Chassis | Dell PowerEdge R640 (1U) |
| CPU | 2× Intel Xeon Gold 6142 — **32 cores / 64 threads**, dual-socket NUMA |
| RAM | **512 GB DDR4 ECC** |
| Boot | 2× consumer NVMe on a PCIe card, **RAID1 + LVM**, ~150 GB free |
| SSD tier | 3× SATA SSD in **ZFS RAID-Z1**, **1.8 TB usable** |
| HDD tier | 4× SAS HDD, 1.8 TB each — **installed but unconfigured**, ~5.4 TB usable as RAID-Z1 |
| Network | 10 G SFP+ (data) + 1 G RJ45 (iDRAC) — see §4.1 |

**Verify before starting:**

- **The HPE 5130 is the EI feature set** (JG937A should be a 5130-48G-PoE+-4SFP+ EI). Stage 0
  needs `interface Vlan-interface N` to accept an `ip address`, plus `packet-filter`,
  `dhcp select relay` and ideally `configuration replace file`. The SI variants are more
  limited. Check with `display version` / `display device manuinfo`.
- **The PERC is in HBA/IT (non-RAID) mode** and presents all four SAS drives as raw devices.
  ZFS requires it — presumably already true given the SATA SSDs are genuinely ZFS, but each
  SAS drive may still need explicit conversion to non-RAID.

### 2.2 What this means

**RAM and CPU are effectively free. Fast storage is the binding constraint; bulk storage is
not.**

With 512 GB you can run as many control-plane and worker VMs as you like and stop worrying
about the memory footprint of monitoring. The SAS drives remove the capacity crunch for bulk
data, but they are **spinning disks** — they do not relieve pressure on the 1.8 TB SSD tier,
where anything latency-sensitive has to live. See §2.3 for tiering and §2.6 for the budget.

Consequences, in order of importance:

- **RAID-Z1 already provides disk redundancy on both tiers.** One drive per pool can fail
  without data loss. Any replication added at the Kubernetes layer (Longhorn, Ceph) is
  therefore **not buying protection from disk failure** — it buys protection from VM loss and
  logical corruption, at a 2–3× multiplier. On the SSD tier that trade is not worth it (D7).
- **There is still no hardware fault tolerance.** Three control-plane VMs, 3-replica storage
  and a 3-instance Postgres cluster all die together when the box dies. Multi-instance setups
  buy zero-downtime rolling upgrades, fast recovery from a single VM/pod failure, and
  protection from one replica corrupting — all genuinely useful, none of them availability
  against host loss. Budget accordingly.
- **RAID-Z is a poor fit for VM disks and databases.** Two distinct problems:
  - *Padding overhead.* On 3-disk RAID-Z1 with `ashift=12`, an 8K `volblocksize` consumes
    **~166% of nominal space**. Proxmox changed its default to 16K (PVE 8.1+), which brings
    this back to the expected ~133%. **Verify the SSD pool is on 16K, not 8K.**
  - *Read-modify-write.* 16K `volblocksize` conflicts with PostgreSQL's 8K pages, causing
    write amplification. There is no way to have both on a single RAID-Z1 vdev — see §2.7.
- **The boot NVMe is fast but untrustworthy.** Consumer NVMe without power-loss protection has
  poor *sync*-write latency (it can't safely use its DRAM cache) and low endurance. etcd and
  PostgreSQL are relentlessly sync-write heavy, so that would be the worst placement available
  despite the drives being nominally the fastest. It is also LVM, not ZFS: no checksums, no
  snapshots, no `send`/`recv`. **Use it only for regenerable data** (§2.3).
- **Node count is bounded by SSD-tier disk, not RAM.** Every node needs a root disk out of a
  1.44 TB working budget that also holds every PVC. With 512 GB you will never run out of
  memory; you can absolutely fill `ssdpool`. This is why the node pools are a fixed, budgeted
  size (§2.6, §6.6) rather than something that grows on demand.
- **Keep every pool under ~80% full.** ZFS performance and fragmentation degrade badly past
  that, so working budgets are **~1.44 TB** (SSD) and **~4.32 TB** (HDD).
- **Dual-socket NUMA.** Large VMs should either fit within one NUMA node or have NUMA topology
  exposed in Proxmox, or you'll pay a memory-latency penalty. Relevant for the bigger workers.
- **The Proxmox host is the largest SPOF and is not managed by stages 2 or 3.** That's why
  stage 1 configures it as code rather than leaving it hand-tuned.

**Corollary — the backup rule:** at least one copy of anything you care about (Postgres
backups, PVC snapshots, the Git repo, the secrets root key) must live **off this machine**.
RAID-Z1 is not a backup. See §7.7.

### 2.3 Storage architecture — three tiers, host-managed

All storage stays under Proxmox's ZFS. No TrueNAS VM — rationale in §2.4.

| Tier | Devices | Layout | Usable | Role |
|---|---|---|---|---|
| **boot** | 2× consumer NVMe | RAID1 + LVM | ~150 GB free | Proxmox OS, ISOs, CT templates, **PBS chunk cache** — regenerable data *only* |
| **`ssdpool`** | 3× SATA SSD | ZFS RAID-Z1 *(existing)* | 1.8 TB | VM disks, etcd, PostgreSQL, hot RWO PVs |
| **`hddpool`** | 4× SAS HDD | ZFS RAID-Z1 *(to create)* | ~5.4 TB | NAS datasets, bulk/cold PVs, NFS RWX exports, **PBS local datastore** |

This maps onto Kubernetes as **two StorageClasses plus a static NFS export**. Proxmox CSI
exposes each Proxmox storage separately, so tiering costs nothing extra:

| Volume kind | Backed by | Access | For |
|---|---|---|---|
| `proxmox-ssd` (StorageClass) | `ssdpool` | RWO | PostgreSQL, metrics TSDB, anything latency-sensitive |
| `proxmox-hdd` (StorageClass) | `hddpool` | RWO | Media, Loki chunks, bulk app data |
| `nfs-nas` (static PV/PVC) | `hddpool/nas` via the `nas` LXC | RWX | Nextcloud, the odd shared volume |

RWX is deliberately *not* a StorageClass — no CSI driver, no dynamic provisioning. For the
handful of shared volumes a homelab actually needs, each is a **static PV/PVC pair defined in
Git** pointing at the NFS export. One fewer controller in the cluster, at the cost of writing
a PV by hand instead of just requesting a PVC.

#### Why the boot NVMe holds no VM disks

It is the fastest device, but: it **doesn't fit** (VM disks budget to ~370 GB, §2.6, against
~150 GB free); consumer NVMe without PLP is **the wrong device for etcd and Postgres**
(sync-write latency and endurance); and it is **LVM, not ZFS**, so no checksums, snapshots or
`send`/`recv`.

Its best use is regenerable data: ISOs, container templates, and the PBS chunk cache. If those
drives die you lose a cache, not data — exactly what you want from storage you don't fully
trust. Monitor wear anyway.

#### The NAS, and why it also solves RWX

A single dataset serves both needs, which is the tidiest part of this design:

- `hddpool/nas` is exported by a **lightweight `nas` LXC running Samba + NFS**, configured by
  stage 1 Ansible. Being outside Kubernetes, **the file shares keep working when the cluster is
  down** — which matters for a household NAS.
- The **same NFS export** backs the `nfs-nas` static RWX volumes — not even a CSI driver.
- "Cloud storage" (sync clients, sharing, mobile apps) is **Nextcloud in the cluster** on a
  static `nfs-nas` volume pointed at that dataset — so SMB users and cluster workloads see the
  *same files*, rather than two copies to reconcile.

### 2.4 Why not TrueNAS (and when it would be right)

TrueNAS SCALE is a good NAS product, and "TrueNAS VM owns the disks, serves PVs over
NFS/iSCSI via democratic-csi" is a well-trodden pattern. It is ruled out here by one hard
constraint plus three softer ones.

**The hard one — you cannot pass through only the HDDs.** PCIe passthrough works at
IOMMU-group granularity: you pass the *entire* controller, and every disk attached to it goes
with it. The SATA SSDs and SAS HDDs are almost certainly on the same PERC, so passing it to
TrueNAS would take `ssdpool` — and therefore every VM disk — away from Proxmox. Passing
individual disks instead (`qm set -scsiN /dev/disk/by-id/...`) is explicitly discouraged:
TrueNAS loses SMART access and direct device control, and it is a known route to pool
corruption. **This only becomes viable with a second, separate HBA.**

The softer ones, which apply even with a second controller: a **second ZFS layer with its own
ARC**, duplicating what the host already does well; a **SPOF in the PV data path** (every PV
goes offline while the NAS VM reboots); and **it isn't GitOps** — TrueNAS config is UI-driven
and would be the one part of the estate not described by this repo.

What you give up is a polished web UI for shares, snapshot browsing and user management. The
`nas` LXC covers the function, not the presentation. Revisit only if that UI turns out to
matter — it also needs an extra HBA, which is cheap.

### 2.5 Why `ssdpool` stays a single 3-disk pool

A dedicated mirror vdev for PostgreSQL (2 extra SSDs, matched 8K `volblocksize`, no RAID-Z
parity padding or read-modify-write cost) would cleanly fix the tension in §2.7 — but it isn't
worth it here. Actual load is on the order of **~30 transactions/second**, well within what a
single SSD absorbs even with RAID-Z1's write-amplification tax, and 512 GB of RAM already gives
a large ZFS ARC and generous `shared_buffers` for the read side. Buying 2–3 drives to fix a
cost you can't measure isn't a good trade.

`ssdpool` therefore stays the existing 3× SATA SSD RAID-Z1, hosting VM disks, etcd and
PostgreSQL together.

If load ever grows enough to revisit: `zpool attach` (RAIDZ expansion, OpenZFS 2.3+, in Proxmox
VE 9) can grow `ssdpool` live, one disk at a time, without a rebuild — but only within the same
RAID-Z1 parity scheme. That adds capacity, not the fix; the block-size tension is only removed
by a mirror, which needs a fresh pool built from scratch.

### 2.6 Capacity budget

Talos keeps node disks small, which helps considerably on the SSD tier.

**Boot NVMe — ~150 GB free, regenerable data only**

| Item | Total |
|---|---|
| ISOs, container templates, Talos images | ~40 GB |
| PBS chunk cache (§7.7) | ~80 GB |
| Headroom | ~30 GB |

**`ssdpool` — 1.8 TB usable, ~1.44 TB working budget (80%)**

| Item | Count | Each | Total |
|---|---|---|---|
| Stage 1 LXCs (`netcore`, `runner`, `nas`) | 3 | 10 GB | 30 GB |
| Control plane (Talos) | 3 | 20 GB | 60 GB |
| Workers (Talos) | 3 | 60 GB | 180 GB |
| **VM subtotal** | | | **~270 GB** |
| **Remaining for hot RWO PVs (`proxmox-ssd`)** | | | **~1.17 TB** |

Workers get 60 GB rather than the 40 GB an autoscaled design would have used: with one fixed
pool carrying everything there is no burst pool to reserve headroom for, so the budget is
better spent on generous per-node ephemeral storage (image cache, `emptyDir`, container logs)
than left unallocated.

**`hddpool` — ~5.4 TB usable, ~4.32 TB working budget (80%)**

| Item | Total |
|---|---|
| PBS local datastore (§7.7) | ~1.0 TB |
| NAS datasets (`hddpool/nas`, SMB + `nfs-nas` RWX) | ~1.2 TB |
| Bulk/cold RWO PVs (`proxmox-hdd`) — media, Loki chunks | ~0.6 TB |
| Headroom | ~1.5 TB |

Replication multipliers decide D7 on the **SSD tier**, where headroom is finite:

| Storage choice | Multiplier | Usable hot application data |
|---|---|---|
| **Proxmox CSI** (ZFS-backed, no extra replication) | 1× | **~1.17 TB** |
| Longhorn, 2 replicas | 2× | ~585 GB |
| Longhorn, 3 replicas (default) | 3× | ~390 GB |
| Rook-Ceph, 3× replication | 3× | ~390 GB, *and* Ceph OSDs on zvols means CoW-on-CoW |

RAM was never going to decide this. Disk does, and decisively.

### 2.7 The PostgreSQL block-size tension — accepted

- Postgres writes 8K pages.
- 3-disk RAID-Z1 wants ≥16K `volblocksize` to avoid padding waste.
- 16K blocks + 8K writes = read-modify-write amplification on every page write.

**Accepted, on 16K `volblocksize`.** The theoretical fix — a dedicated mirror vdev with matched
8K blocks (§2.5) — has no RMW cost at all, but isn't worth the extra drives: at ~30 tps, even
doubled write cost is nowhere near what a single SSD can absorb, and 512 GB of RAM covers most
of the read side anyway. `full_page_writes=off` (safe specifically because ZFS's own writes are
atomic) remains available as a further knob if this ever needs revisiting.

---

## 3. Target architecture

```
  ── STAGE 0 ──────────────────────────────────────────────────────────
   OPNsense ──1G transit, VLAN 99──► HPE 5130 (L3: SVIs v10/v20/v40)
   NAT + north-south firewall        east-west routing + ACLs, wire speed
   DHCP server + resolver + VPN      10G SFP+ ─► Proxmox (trunk, PVID 10)
                                     10G SFP+ ─► living room (access, v40)
┌─────────────────────────────────────────────────────────────────────┐
│ Proxmox VE host (single node)          vmbr0 vlan-aware, VLAN 10/20 │
│ configured by Stage 1 Ansible: ZFS, bridges/VLANs, firewall, API    │
│ tokens, PBS                                                         │
│                                                                     │
│  storage tiers:  boot NVMe (ISOs, PBS cache)                        │
│                  ssdpool  1.8 TB  → VM disks, hot PVs               │
│                  hddpool  5.4 TB  → NAS, bulk PVs, PBS local        │
│                                                                     │
│  ── STAGE 1 ─────────────────────────────────────────────────────── │
│  ┌──────────────┐ ┌─────────────────────┐ ┌──────────────┐          │
│  │ LXC: netcore │ │ LXC: runner  (v10)  │ │ LXC: nas     │          │
│  │ authoritative│ │ Semaphore UI, runs  │ │ SMB + NFS on │          │
│  │ internal DNS │ │ Ansible, OpenTofu,  │ │ hddpool/nas  │          │
│  │ zone  (v10)  │ │ talhelper, talosctl │ │  (VLAN 20)   │          │
│  └──────────────┘ └──────────┬──────────┘ └──────┬───────┘          │
│                              │                   │                  │
│  ── STAGE 2 (Git-defined, triggered from Semaphore) ─────────────── │
│   OpenTofu  → Proxmox VMs (disks, NIC, VLAN tag)  │                 │
│   talhelper → Talos machine configs, one file     │ NFS → nfs-nas   │
│   talosctl  → apply · bootstrap · upgrade         │       (RWX)     │
│                              ▼                    │                 │
│  ┌────────────────────────────────────────────┐   │                 │
│  │ Workload Kubernetes cluster (VMs, VLAN 20) │◄──┘                 │
│  │  control plane ×3 (Talos VIP) │ workers ×3 │                     │
│  │  ── STAGE 3 (GitOps, workload Flux) ────── │                     │
│  │  Cilium · CSI (ssd/hdd) · Gateway ×2       │                     │
│  │  cert-manager · external-dns · monitoring  │                     │
│  │  CloudNativePG · Nextcloud                 │                     │
│  └────────────────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────────┘
        │ PBS local datastore (hddpool) ──sync──► Backblaze B2
        │ CNPG / Velero / restic ─────────────► Backblaze B2
```

---

## 4. Stage 0 — Network foundation

**Tool:** Ansible, run from your **workstation only**, triggered by hand.
**Property:** every layer above depends on it, and nothing above it can repair it.

Stage 0 is genuinely a layer *below* the bootstrap: stage 1 Ansible reaches the Proxmox host
over this network, so the network cannot be one of the things stage 1 configures.

> **Stage 0 is never GitOps.** A reconciler that manages the network it depends on is a
> circular dependency with an unrecoverable failure mode: push a bad ACL, lose contact with
> the switch, and the controller that would roll it back can no longer reach it. A job that
> rebuilds a VM is recoverable; Flux bricking the default gateway is not.
>
> For the same reason, **stage 0 must not run from the `runner` LXC** (§5.3) — the runner sits
> behind the switch it would be reconfiguring, on a host whose management VLAN it might be
> changing.

### 4.1 The hardware, and the one decision that shapes everything

| Device | Role | Links |
|---|---|---|
| OPNsense | WAN termination, NAT, north-south firewall, DHCP server, resolver, WireGuard | 4× 1 GbE — 1 WAN, 1 LAN, 2 spare |
| HPE 5130 (JG937A) | Core L2/L3 switch | 48× 1 GbE + 4× SFP+ 10 G |
| Living-room switch | **unmanaged**, 10 G SFP+ uplink | PC, household devices, **wireless APs** |
| R640 | Proxmox host | 10 G SFP+ (data) + 1 G RJ45 (iDRAC) |

The requirement was "separate the network infrastructure, the server VMs and the client
devices, **but keep a 10 Gb path between the server and the PC**". Those pull in opposite
directions if the router is the separation point: the router is on a 1 Gb link, so any
inter-VLAN traffic through it is capped at 1 Gb.

**Resolution: the HPE 5130 does the inter-VLAN routing, not OPNsense.** It is a layer-3 switch;
VLAN interfaces (SVIs) forward in the ASIC at wire speed, so server↔PC stays at 10 Gb even
across VLANs. The firewall job then splits in two:

| Direction | Enforced by | Character |
|---|---|---|
| **North-south** — anything ↔ internet | OPNsense | stateful, Suricata-capable, unchanged from today |
| **East-west** — VLAN ↔ VLAN | 5130 packet-filter ACLs | stateless, hardware, wire-speed |

**The accepted trade-off:** east-west traffic never reaches OPNsense, so there is no stateful
inspection, no IDS and no logging between VLANs — only stateless ACLs. §4.5 makes that far less
painful than it sounds. The alternative (router-on-a-stick) is ruled out by the 10 Gb
requirement, and putting a 10 G NIC in OPNsense would not fix it either: pf realistically
forwards 2–5 Gbps per flow, so PC↔NAS transfers would be capped by router CPU instead of link
speed. The switch ASIC is free.

```
                       ISP
                        │
                 ┌──────┴───────┐
                 │  OPNsense    │  WAN: DHCP from ISP        2 spare 1G ports
                 │  10.100.99.1 │  NAT · north-south FW · DHCP · resolver · WireGuard
                 └──────┬───────┘
                        │ 1 GbE — transit ONLY, VLAN 99, 10.100.99.0/30
                 ┌──────┴──────────────────────────────┐
                 │  HPE 5130 EI          10.100.99.2   │
                 │  SVIs: v10 v20 v40 v99 (v50 later)  │ ← east-west routing
                 │  ip route-static 0.0.0.0 0 …99.1    │   + packet-filter ACLs
                 └───┬─────────────────────────┬───────┘
         10G SFP+    │                         │  10G SFP+ access, VLAN 40
            trunk    │                         │
       ┌─────────────┴────────┐      ┌─────────┴──────────┐
       │ R640 — Proxmox       │      │ living-room switch │
       │ vmbr0 (vlan-aware)   │      │  (unmanaged)       │
       │  PVID 10: host, LXCs │      │  PC, TV, APs…      │
       │  tag 20: VMs, nodes  │      │  all in VLAN 40    │
       │ iDRAC → 1G RJ45, v10 │      └────────────────────┘
       └──────────────────────┘
```

The router sits in a **/30 transit VLAN and is in no user subnet**. This is deliberate: if
OPNsense shared a segment with hosts whose default gateway is the switch SVI, every
internet-bound packet would provoke an ICMP redirect and hairpin across the segment. One /30
removes the entire class of problem, and has the pleasant side effect that **OPNsense needs no
802.1Q configuration at all** — its LAN port is a plain access port with a /30 on it.

### 4.2 VLAN and address plan

Keeps the existing `10.100.0.0/16` supernet, with **VLAN ID = third octet**. This costs a
one-time renumber of four infrastructure addresses (router, switch, iDRAC, PVE) and is worth
it — nothing is built on top of them yet.

| VLAN | Subnet | Switch SVI | Holds |
|---|---|---|---|
| 1 | — | *none* | **Quarantine.** No SVI, no route. All unused ports park here. |
| 10 | `10.100.10.0/24` | `.10.2` (gateway) | Infra: switch `.2`, `netcore` `.10`, `runner` `.11`, iDRAC `.40`, PVE host `.41` |
| 20 | `10.100.20.0/24` | `.20.2` (gateway) | Server workloads: Kubernetes nodes, `nas` LXC, **LB VIPs `.200–.250`** |
| 40 | `10.100.40.0/24` | `.40.2` (gateway) | Clients: PC, laptops, phones, TV, APs — everything behind the living-room switch. All DHCP, no reservations |
| 50 | `10.100.50.0/24` | `.50.2` (gateway) | IoT / guest — **deferred**, see §4.8 |
| 99 | `10.100.99.0/30` | `.99.2` | Transit only: OPNsense `.1` ↔ switch `.2`. No hosts. |
| — | `10.100.98.0/24` | *none* | WireGuard tunnel subnet, lives entirely on OPNsense (§4.6) |

Two notes on what is deliberately *absent*:

- **No separate LB-VIP VLAN.** Cilium LB-IPAM with L2 announcements requires the VIP to be in
  the same subnet as the interface announcing it, so a dedicated VIP VLAN would mean a second
  NIC on every Kubernetes node for no benefit. The VIP range is carved out of VLAN 20. → D10.
- **No storage/replication VLAN.** With Proxmox CSI and a NAS LXC on the same host (D7), there
  is no storage replication traffic to isolate.

Within VLAN 20, addresses are hand-allocated because Talos node addresses are written
statically into `talconfig.yaml` (§6.1) rather than handed out by a controller:

| Range | Holds |
|---|---|
| `.20.10` | Talos control-plane VIP (D10) |
| `.20.11–.20.13` | Control-plane nodes |
| `.20.21–.20.23` | Worker nodes |
| `.20.30` | `nas` LXC |
| `.20.200–.20.250` | Cilium LB-IPAM pool |
| `.20.100–.20.199` | DHCP pool — first boot of a node before its config is applied, and anything transient |

The `nas` LXC lives in VLAN 20, not VLAN 10, even though it is a stage 1 component: it is a
service consumed by clients (SMB) and by the cluster (NFS), so it belongs with the server
workloads rather than with the infrastructure that manages them.

### 4.3 Port map — access vs trunk

Only devices that must be in **more than one VLAN at once** need to know VLANs exist. On an
access port the switch adds the tag on ingress and strips it on egress, so the attached device
sees ordinary untagged Ethernet and needs no configuration whatsoever.

| Port | Type | VLAN(s) | Config needed on the device |
|---|---|---|---|
| GE → OPNsense LAN | access | 99 | none — set LAN to `10.100.99.1/30` |
| SFP+ → living-room switch | access | 40 | none — the unmanaged switch stays unmanaged |
| SFP+ → R640 | **trunk** | PVID 10, tagged 20, 50 | `/etc/network/interfaces` (below) |
| GE → iDRAC | access | 10 | none — leave iDRAC's own VLAN setting disabled |
| all unused | access | 1 | — |

**Exactly one trunk in the whole estate.** VLAN 50 is pre-permitted so enabling it later needs
no port change. Make VLAN 10 the PVID (untagged) rather than tagging everything:

```
interface Ten-GigabitEthernet1/0/49            # Proxmox
 port link-type trunk
 port trunk permit vlan 10 20 50
 port trunk pvid vlan 10
 undo port trunk permit vlan 1
```

That lets the Proxmox host keep the flat configuration a stock install already has — no VLAN
sub-interface — while VMs select their VLAN purely as a Proxmox NIC property:

```
auto vmbr0
iface vmbr0 inet static
    address 10.100.10.41/24
    gateway 10.100.10.2
    bridge-ports enp1s0f0
    bridge-vlan-aware yes
    bridge-vids 2-4094
```

VMs then get `net0: virtio,bridge=vmbr0,tag=20`; nothing inside the guest changes, so Talos
simply sees an untagged NIC. The purist objection to untagged management on a trunk is VLAN
hopping, which requires an attacker who already has root on the Proxmox host — at which point
the tag is not the relevant control.

**This is also what makes the cutover safe.** Configure the switch port as a trunk with PVID 10
*before* touching the server: the host's existing untagged traffic keeps flowing, so you are
not reconfiguring both ends of a live link simultaneously.

Optional: jumbo frames (MTU 9000) on the 10 G path help NFS and backup throughput, but every
interface in a given VLAN must agree, and routed traffic must agree end to end. Leave it at
1500 until the network is otherwise stable.

### 4.4 OPNsense — what changes

Very little, but these are load-bearing and are the classic way this design fails silently:

- **Static route** `10.100.0.0/16 → 10.100.99.2`.
- **Manual outbound NAT rule for `10.100.0.0/16`.** Automatic outbound NAT covers only
  *directly connected* networks, so with the router in a /30 transit VLAN, every real VLAN gets
  no internet at all until this exists.
- **Firewall rules on the transit interface must permit source `10.100.0.0/16`**, not just
  the /30.
- **WAN rule permitting UDP 51820** for WireGuard (§4.6).

Per-VLAN **internet egress policy still lives on OPNsense** and is still stateful — it matches
on source subnet exactly as before. Only east-west policy moved to the switch.

### 4.5 East-west policy — the trust hierarchy

Stateless ACLs are usually painful because you must hand-write return-traffic rules. A trust
ordering removes almost all of that:

```
mgmt (10) ≈ clients (40) ≈ vpn (98)  >  servers (20)  >  iot (50)
```

**Clients and the VPN are a trusted tier, level with mgmt.** Anything on VLAN 40 — and any
WireGuard peer — reaches mgmt, servers and iot without exception: Proxmox UI, iDRAC, the job
runner, SMB, Gateways, all of it. There is no per-host carve-out and nothing needs a static
address or a DHCP reservation.

ACLs are applied **inbound on each VLAN interface**, i.e. to traffic *sourced by* that VLAN.
Higher tiers may initiate downward freely; lower tiers get explicit permits only. The trick:
if `servers → clients` is denied at VLAN 20's ingress, connections from 20 into 40 never exist,
so there is never any return traffic to match — VLAN 40's ingress permits everything anyway.
Explicit `established`/TCP-flag matching is then needed only for the handful of asymmetric
permits.

| From | To | Policy |
|---|---|---|
| mgmt (10) | anywhere | permit |
| clients (40) | anywhere | permit |
| vpn (98) | anywhere | permit |
| servers (20) | mgmt (10) | **deny**, except the four permits below |
| servers (20) | clients (40) | deny except established |
| iot (50) | anything internal | deny |
| any | internet | permit — OPNsense filters it |

The practical consequence is that **the entire east-west policy is one ACL**, on VLAN 20 — plus
a second on VLAN 50 if and when IoT separation lands (§4.8). VLAN 10, VLAN 40 and the transit
interface carry **no packet-filter at all**, because every one of them permits everything.

**The `servers → mgmt` permits.** The cluster genuinely needs four paths upward, and omitting
them is a silent-failure trap:

| Permit | Why |
|---|---|
| `→ 10.100.10.41:8006` | Proxmox API, needed by Proxmox CSI (§7.4) |
| `→ 10.100.10.10:53` (udp/tcp) and `:8081` | `external-dns` writing records into `netcore` (§4.6, §7.3) |
| `→ 10.100.10.41:9221` | Prometheus/vmagent scraping the PVE exporter (§5.1, §7.5) |
| `→ 10.100.10.40:443` | Prometheus/vmagent scraping iDRAC Redfish (§5.1, §7.5) |

Worked example — the only ACL the design actually needs:

```
acl advanced 3020
 rule 5   permit udp destination-port eq bootps                                    # DHCP relay
 rule 10  permit tcp destination 10.100.10.41 0 destination-port eq 8006           # Proxmox API
 rule 20  permit udp destination 10.100.10.10 0 destination-port eq dns            # PowerDNS
 rule 21  permit tcp destination 10.100.10.10 0 destination-port eq dns
 rule 22  permit tcp destination 10.100.10.10 0 destination-port eq 8081           # PowerDNS API
 rule 30  permit tcp destination 10.100.10.41 0 destination-port eq 9221           # PVE exporter
 rule 40  permit tcp destination 10.100.10.40 0 destination-port eq 443            # iDRAC Redfish
 rule 50  deny   ip  destination 10.100.10.0 0.0.0.255
 rule 60  permit tcp destination 10.100.40.0 0.0.0.255 established                 # replies to clients
 rule 70  deny   ip  destination 10.100.40.0 0.0.0.255
 rule 100 permit ip                                                                # internet
interface Vlan-interface 20
 packet-filter 3020 inbound
```

Four things that are easy to get wrong:

1. **Rule 5 is not optional.** DHCP relay (§4.6) breaks without it, and the symptom — VMs
   getting no address — looks nothing like an ACL problem. It belongs in every ACL on a VLAN
   with `dhcp: true`.
2. **Rule 60 covers TCP return traffic only.** A stateless ACL cannot infer UDP flows, so a
   UDP service on VLAN 20 that a client consumes — HTTP/3 on a Gateway is the realistic one —
   needs an explicit `permit udp … source-port eq <port>` alongside it, or it will fail in the
   confusing way where TCP works and QUIC silently doesn't.
3. **Do not put an ACL on the transit interface.** All inbound internet return traffic for the
   entire estate arrives on Vlan-interface 99; anything short of `permit ip` there breaks the
   whole house. The trusted-client policy means it needs no ACL, which removes the trap
   entirely — leave it alone.
4. **End every ACL with an explicit `permit ip`** rather than relying on Comware's implicit
   default action for unmatched packets, and generate rule numbers with gaps (10, 20, 30…) so
   inserting a rule later does not renumber everything below it.

The 5130's ACL capacity is TCAM-bound; a policy of this size is comfortably within it, but do
not plan on thousands of rules.

### 4.6 DHCP, DNS and VPN all stay on OPNsense — except the internal zone

**DHCP: OPNsense, reached by relay.** The switch relays per VLAN:

```
dhcp enable
interface Vlan-interface 40
 dhcp select relay
 dhcp relay server-address 10.100.99.1
```

Kea on OPNsense matches subnets by `giaddr`, so define `10.100.20.0/24`, `10.100.40.0/24` (and
later `.50.0/24`) as subnets with no interface binding. Two reasons this beats moving DHCP into
`netcore`:

1. **Household DHCP must not live on the machine this project exists to rebuild.** OPNsense is
   the most always-on box in the house; the Proxmox host will be torn down and re-provisioned
   repeatedly.
2. Kubernetes node addresses are static, written into `talconfig.yaml` (§4.2, §6.1), so DHCP
   only serves a node's very first boot — before its machine config is applied — plus
   household devices. Low value, high blast radius.

**DNS: this is `netcore`'s actual job.** The real requirement was never DHCP; it is that stage
3's `external-dns` must *write* records into an internal zone, and OPNsense's Unbound has no
good dynamic-update story. So:

- `netcore` (`10.100.10.10`) runs **PowerDNS**, authoritative for `internal.<domain>` **only**.
- Unbound on OPNsense stays the resolver for every client, with a domain override for that one
  zone pointing at `netcore`.
- Clients keep pointing at OPNsense via DHCP. Nothing else changes.

The failure mode is good: if the Proxmox host is down, the internal zone goes with it — but
every name in that zone resolves to cluster Gateways that are also down. General internet DNS
never breaks.

**VPN: WireGuard on OPNsense**, tunnel subnet `10.100.98.0/24`. The tunnel is a trusted tier
(§4.5), so every peer reaches mgmt, servers and iot exactly as a client on VLAN 40 does — no
peer needs a fixed address and the switch needs no per-peer rules. OPNsense already knows the
tunnel subnet directly; the switch reaches it via the default static route, so no extra routing
is needed.

### 4.7 Codifying stage 0 — one source of truth

Both devices can be driven from the repo. The reproducibility is nice; the **single source of
truth is the real payoff**. Without it the VLAN model is hand-maintained in at least five
places — 5130 SVIs and ACLs, OPNsense static route/NAT/rules, Kea subnets, Proxmox
`bridge-vids` and VM tags, and Cilium's LB-IPAM pool — and they will drift.

```
stage0-network/
├── group_vars/all/network.yml     # ← the ONLY place VLANs/subnets/policy are written
├── roles/
│   ├── opnsense/                  # REST API
│   └── comware/templates/5130.cfg.j2
└── playbooks/
    ├── verify.yml                 # read-only: pull running config, diff, fail on drift
    └── apply.yml                  # explicit confirmation required
```

```yaml
vlans:
  - { id: 10, name: mgmt,    subnet: 10.100.10.0/24, svi: .2, dhcp: false }
  - { id: 20, name: servers, subnet: 10.100.20.0/24, svi: .2, dhcp: true,
      dhcp_pool: 10.100.20.100-10.100.20.199,      # first boot only — nodes are static
      lb_pool:   10.100.20.200-10.100.20.250 }
  - { id: 40, name: clients, subnet: 10.100.40.0/24, svi: .2, dhcp: true }
  - { id: 99, name: transit, subnet: 10.100.99.0/30, svi: .2, dhcp: false }
vpn:
  subnet: 10.100.98.0/24
policy:
  # trusted tiers — these render no ACL at all (§4.5)
  - { from: mgmt,    to: any, action: permit }
  - { from: clients, to: any, action: permit }
  - { from: vpn,     to: any, action: permit }
  - { from: servers, to: clients, action: deny, allow_established: true }
  - { from: servers, to: mgmt,    action: deny,
      except_dst: ["10.100.10.41:8006", "10.100.10.10:53", "10.100.10.10:8081",
                   "10.100.10.41:9221", "10.100.10.40:443"] }
```

Stage 1 reads the same file for `bridge-vids`; stage 2's `talconfig.yaml` node addresses must
sit outside `dhcp_pool` and outside `lb_pool`; stage 3 generates its
`CiliumLoadBalancerIPPool` from `lb_pool`.

**OPNsense.** Split it: **bootstrap** (interface assignment, LAN IP, enabling the API) is a
one-time manual job — automating it is where a weekend disappears. **Everything mutable** —
filter rules, aliases, outbound NAT, the static route, Unbound overrides, WireGuard peers, Kea
subnets and reservations — goes through the REST API from Ansible. The `ansibleguy.opnsense`
collection covers most of this; **verify its current Kea coverage**, since Kea replaced ISC
dhcpd relatively recently and module support may lag.

Independently, turn on OPNsense's **built-in Git backup** (*System → Configuration → Backups →
Git*). It commits `config.xml` on every change, giving versioning, a diff trail and a restore
path for a few minutes of work — and it is how you will notice the change you made in the UI at
1am that the playbooks do not know about.

**Comware.** The Ansible collections for Comware/H3C have a chequered maintenance history;
don't build on one without first checking it is alive. The durable approach needs none of them:

1. Render the full switch config from Jinja2 into `5130.cfg`.
2. `scp` it to `flash:/`.
3. Apply with `configuration replace file flash:/5130.cfg` — Comware 7 diffs against the
   running config and applies the delta without a reboot. **Confirm this exists on your
   firmware before relying on it.**
4. Verify reachability, then `save`.

`display current-configuration` compared against the rendered file gives both a `--check` mode
and a CI drift check. Credentials belong in the private secrets repo (§8.1) — or better, use
Comware's SSH public-key authentication and store no password at all.

### 4.8 Break-glass, and the deferred items

**Arm a self-healing rollback before every apply.** On the switch:

```
scheduler job SAFETY
 command 1 reboot force
scheduler schedule SAFETY-NET
 job SAFETY
 time once delay 00:10
```

If the new config locks you out, the switch reboots in ten minutes to the last **saved**
config. On success, `undo scheduler schedule SAFETY-NET` and then `save`. (Syntax approximate —
confirm against your firmware.)

**Keep a USB-serial adapter.** VLAN 10 carries iDRAC *and* the Proxmox host *and* the switch's
own management address, so a bad VLAN 10 change removes every remote path simultaneously.
Serial to the 5130 and physical console on OPNsense are the only true fallbacks.

**Deferred — IoT/guest separation (VLAN 50).** The wireless APs hang off the unmanaged
living-room switch, so **every phone and smart plug is in VLAN 40 alongside the workstation** —
and VLAN 40 is a trusted tier (§4.5), so they inherit unfiltered reach into mgmt and servers.
That is accepted for now, and it is the main reason to do this eventually. In increasing cost:

1. **Run a second cable** from the 5130 to the living room as an access port in VLAN 50.
2. **Replace the living-room switch** with a managed one and make the uplink a trunk.
3. **Move the APs onto the 5130 directly** — the real answer for wireless IoT, since a
   VLAN-aware AP maps SSID → VLAN and "IoT" and "Home" become two SSIDs on one radio. It needs
   the AP on a trunk port, which an unmanaged switch downstream cannot cleanly provide.

Option 3 is where this ends up, so prefer it if cable is being run anyway.

**Deferred — 2 spare OPNsense ports.** Nothing needs them. If WAN ever exceeds 1 Gb, LACP two
of them to the switch; note that this raises aggregate throughput, not per-flow.

### Stage 0 exit criteria

- [ ] `verify.yml` reports zero drift on both devices from a clean checkout
- [ ] A client in VLAN 40 gets an address via relay and reaches the internet
- [ ] The PC (VLAN 40) sustains ≥ 5 Gbit/s to a VM in VLAN 20 — proving the traffic is switched,
      not routed through OPNsense
- [ ] Any DHCP-addressed VLAN 40 host reaches the Proxmox UI, iDRAC and the job runner, with
      no per-host exception and no static address anywhere
- [ ] The hierarchy holds downward: a VLAN 20 host cannot reach `10.100.10.0/24` outside the
      four `servers → mgmt` permits, and cannot initiate to VLAN 40
- [ ] A WireGuard peer connects and has the same reach as a host on VLAN 40
- [ ] Serial console access to the 5130 is tested and documented
- [ ] A deliberately broken config is rolled back by the SAFETY-NET job without intervention

---

## 5. Stage 1 — Bootstrap

**Tool:** Ansible, run from your workstation against the Proxmox host over SSH.
**Property:** idempotent, re-runnable, and functional with the entire Kubernetes estate down.

### 5.1 Proxmox host configuration
- Enterprise repo off / no-subscription repo on, unattended-upgrade policy
- **`pvecm create`** — the single node must be "clustered with itself" before `proxmox-csi-plugin`
  will provision anything (D7). One-time, low-risk, and easy to forget.
- **Create `hddpool`** — 4× SAS HDD as ZFS RAID-Z1 (§2.3). All four are installed; confirm the
  PERC presents them as raw non-RAID devices first (§2.1). Datasets: `hddpool/nas`,
  `hddpool/pbs`, plus a Proxmox storage for `proxmox-hdd` volumes.
- **ZFS tuning — the highest-value items on this hardware:**
  - **`volblocksize` ≥ 16K** on both ZFS storages. On 3-disk RAID-Z1 the old 8K default costs
    ~166% of nominal space (§2.2). Verify the current setting; it applies only to *newly
    created* volumes, so existing disks must be migrated to change it.
  - `recordsize=1M` on `hddpool/nas` for bulk files, rather than the 128K default
  - **ARC cap.** Default ARC is 50% of RAM = 256 GB. Set it explicitly rather than leaving it
    to chance — 64–128 GB leaves plenty for VMs while giving a big read cache.
  - `compression=lz4` (or `zstd`), `atime=off`, autotrim for SSDs
  - Scheduled scrub + SMART monitoring with alerting
- **NUMA:** expose NUMA topology for larger VMs, or size them to fit within one socket (§2.2)
- VLAN-aware `vmbr0`, `bridge-vids` rendered from `network.yml` (§4.3, §4.7)
- Host firewall rules, NTP, email/alert relay
- **iDRAC:** configure out-of-band management and the Redfish exporter, so hardware health
  (PSU, fans, drive SMART, temperatures) lands in the stage 3 monitoring stack
- **PVE exporter** on `:9221` for the same reason
- API token + role for stage 2's OpenTofu (`bpg/proxmox`) and for Proxmox CSI — least
  privilege, not `root@pam`, and separate tokens so the cluster's credential cannot create VMs
- **Proxmox Backup Server** — local datastore on `hddpool` plus an **S3 datastore on Backblaze
  B2** and a sync job between them, client-side encryption on, chunk cache on the boot NVMe (§7.7)

### 5.2 `netcore` LXC — authoritative internal DNS
`10.100.10.10`. Hosts the **internal** DNS zone that stage 3's `external-dns` writes into,
giving split-horizon DNS: internal names resolve to internal Gateway IPs, the same names on the
public internet resolve via Cloudflare. PowerDNS, authoritative for `internal.<domain>` and
nothing else.

It does **not** serve DHCP and is **not** the network's resolver — both stay on OPNsense, which
forwards just this one zone here via a domain override (§4.6). That keeps household DHCP and
general name resolution off the machine this project exists to rebuild repeatedly, and shrinks
`netcore` to the one job OPNsense genuinely cannot do: accept dynamic updates.

### 5.3 `runner` LXC — web UI and job execution
`10.100.10.11`. **Semaphore UI** (D5): the "run stage 2 from a GUI" requirement and the
break-glass console. Outside Kubernetes so it still works when Kubernetes doesn't.

With D2 changed to talhelper, this LXC is **where stage 2 lives** (§6) rather than a
convenience — it is the only thing that can rebuild the cluster, so stage 1 must install and
pin the toolchain rather than leaving it to whatever is on a laptop:

- `tofu` + the `bpg/proxmox` provider, with a provider mirror or lockfile committed
- `talhelper`, `talosctl`, `kubectl`, `flux`, `sops`, `age` — versions pinned in Ansible
- The age private key (§5.5), so `talhelper genconfig` can decrypt `talsecret.sops.yaml`
- Checkouts of both repos (§8.1), and the OpenTofu state backend

It must **not** run stage 0 (§4): it sits behind the switch it would be reconfiguring.

### 5.4 `nas` LXC — SMB + NFS file services
VLAN 20. Bind-mounts `hddpool/nas` and exports it two ways (§2.3):

- **SMB** for household/desktop access — available whether or not Kubernetes is running
- **NFS** as the backing export for the cluster's `nfs-nas` RWX static PVs

Both views are the *same dataset*, so Nextcloud in the cluster and a laptop over SMB see the
same files. Users, shares and exports are defined in Ansible. Snapshots come from ZFS on the
host; backup is restic/rclone to B2 (§7.7). This is greenfield — there is no existing NAS data
to import.

### 5.5 Secrets root
Stage 1 generates the **age** keypair and places the private key in the two places that need
it:

- on the `runner` LXC, so `talhelper genconfig` can decrypt `talsecret.sops.yaml` (§6.1)
- later, as the `sops-age` Secret in `flux-system` on the workload cluster, so Flux can decrypt
  at reconcile time — installed during the stage 2 bootstrap (§6.2), since the cluster does not
  exist yet at this point

It also configures the private secrets repo as a second `GitRepository` source (§8.1).

The private key is the one thing that **cannot** live in any repo — password manager plus an
offline copy. Losing it means every encrypted value in Git is unrecoverable; leaking it means
every encrypted value ever committed is exposed. Back it up before you rely on it. → D11.

### Stage 1 exit criteria
- [ ] Ansible run from a clean Proxmox install reproduces the whole stage without manual steps
- [ ] Both pools present, `volblocksize` ≥ 16K verified
- [ ] The internal DNS zone resolves, and a test dynamic update succeeds
- [ ] `vmbr0` is VLAN-aware and a VM tagged `20` gets an address via relay
- [ ] Semaphore is reachable and able to run a trivial job
- [ ] `tofu`, `talhelper`, `talosctl`, `flux` and `sops` are installed and version-pinned on the
      runner, and `talhelper genconfig` renders configs from an encrypted secret file
- [ ] Secrets root is provisioned and backed up off-box

---

## 6. Stage 2 — Cluster lifecycle

Everything here is declarative source in `stage2-cluster/`, applied by **jobs on the `runner`
LXC** (Semaphore, D5). Unlike stages 0 and 1 it is not "run from a laptop"; unlike stage 3 it is
not continuously reconciled either. It is **Git-defined, triggered on demand or on a schedule**.

### 6.1 Three tools, and the line between them

| Tool | Owns | Source of truth |
|---|---|---|
| **OpenTofu** + `bpg/proxmox` | The VMs: CPU, RAM, disks, NIC and VLAN tag, boot media | `stage2-cluster/tofu/` |
| **talhelper** | Talos machine configuration for every node, rendered from one file | `stage2-cluster/talconfig.yaml` |
| **`talosctl`** | Applying configs, bootstrapping etcd, upgrading Talos and Kubernetes | — |

`talhelper genconfig` renders `talconfig.yaml` plus an encrypted `talsecret.sops.yaml` into
per-node machine configs. That render is pure: the outputs are reproducible from the two inputs,
so **only the inputs are committed**, and the secret one lives in the private repo (§8.1) under
the same age key as everything else. `talhelper validate` gives a schema check in CI, and
`talhelper gencommand` emits the exact `talosctl` invocations, which is what the Semaphore jobs
wrap.

Node addressing is **static, written directly in `talconfig.yaml`** (§4.2). There is no IPAM
controller and no DHCP reservation to keep in sync — DHCP serves only a node's first boot,
before any config is applied.

### 6.2 The bootstrap sequence

Each step is a Semaphore job; the whole chain is one job that runs them in order.

1. **`tofu apply`** — creates 3 control-plane and 3 worker VMs on `ssdpool`, NIC tagged VLAN 20,
   booting the Talos image (§6.3). Memory ballooning **off** on every node VM: ballooning versus
   kubelet's view of available memory is a bad combination, and with 512 GB there is no reason
   to overcommit.
2. **`talhelper genconfig`** — renders machine configs into a gitignored `clusterconfig/`.
3. **`talosctl apply-config --insecure`** to each node at its maintenance-mode address.
4. **`talosctl bootstrap`** against one control-plane node — starts etcd. Exactly once, ever.
5. **`talosctl kubeconfig`** — the cluster is now reachable at the Talos VIP `10.100.20.10`.
6. **Cilium** comes up with the cluster, not after it. Talos is configured `cni: none` with
   `kube-proxy` disabled, and the rendered Cilium manifests are embedded as talhelper
   `inlineManifests`, so the cluster never sits in a NotReady state waiting for a human to run
   `helm install`. Cilium's `k8sServiceHost`/`k8sServicePort` point at **KubePrism**
   (`localhost:7445`), Talos's local API-server proxy — the standard way to give the CNI an API
   endpoint before there is a service network to provide one.
7. **`flux bootstrap`** against `stage3-platform/`, and install the `sops-age` Secret (§5.5).
   From here on, stage 3 is continuously reconciled and stage 2 goes quiet until a version bump.

### 6.3 Node images

Talos on Proxmox needs the **`siderolabs/qemu-guest-agent`** system extension, or Proxmox never
learns the guest's IP and cannot do a graceful shutdown. Extensions are baked in at image build
time by the **Image Factory**, which means an image is identified by a *schematic ID* derived
from the extension list.

talhelper handles this: declare the schematic in `talconfig.yaml` and use
`talhelper genurl installer` / `genurl iso` to produce the matching URLs. This is the piece that
made D1 (Talos) attractive in the first place — **no Packer pipeline to own**, because the image
is a pure function of a few lines of YAML.

Import the `nocloud` image per release into Proxmox as a template or ISO; stage 1 budgets ~40 GB
on the boot NVMe for exactly this (§2.6).

### 6.4 Upgrades — still a version bump in one file

"Upgrades are a version bump in one file, not a runbook" was a design principle before the
tooling changed (§1), and it survives the change intact — only the mechanism differs.

| Upgrade | Change | Then |
|---|---|---|
| Talos | `talosVersion:` in `talconfig.yaml` | `talhelper genurl installer`, then `talosctl upgrade --nodes <n> --image <url>`, one node at a time |
| Kubernetes | `kubernetesVersion:` in `talconfig.yaml` | `talosctl upgrade-k8s --to <version>` — Talos rolls the control-plane components itself |
| Machine config | anything else in `talconfig.yaml` | `talhelper genconfig`, then `talosctl apply-config`; Talos reboots only if the change requires it |

Talos upgrades are A/B: the node boots the new image, and rolls back automatically if it fails
to come up healthy. Sequence control plane before workers, and let etcd report healthy between
nodes — the Semaphore job should assert that rather than trusting a `sleep`.

Renovate can raise the version bumps as pull requests against `talconfig.yaml`, which keeps the
"read the release notes, merge, click Run" loop honest without putting an upgrade controller
inside the cluster.

### 6.5 Certificate rotation

Talos manages its own PKI and rotates Kubernetes control-plane certificates itself, so the
correct answer to an expiring certificate is still **roll the machine**, never "run a rotation
script on a live node".

Two things do not rotate on their own and need to be on the calendar:

- **The `talosconfig` client certificate**, which is what `talosctl` authenticates with. It is
  regenerated by `talhelper genconfig` from the secrets bundle, so the fix is a re-render, not
  a rebuild.
- **The Talos and Kubernetes CAs** in `talsecret.sops.yaml`, which are long-lived.
  `talosctl rotate-ca` handles both if one is ever suspected compromised.

Add a monitoring alert on certificate expiry as a backstop (§7.5).

### 6.6 Fixed capacity, deliberately

There is **no cluster-autoscaler and no burst pool**. Two fixed pools, sized against the §2.6
budget:

| Pool | Count | Root disk | Runs |
|---|---|---|---|
| `control-plane` | 3 | 20 GB | control plane only |
| `workers` | 3 | 60 GB | everything else |

Autoscaling is the wrong shape for this hardware. It exists to convert idle capacity into money
on a cloud bill; here the capacity is a box that is already bought, already powered on, and has
512 GB of RAM that nothing will exhaust. The binding constraint is **SSD-tier disk** (§2.2), and
disk is precisely what autoscaling *consumes* — every burst node takes a root disk out of the
same 1.44 TB budget that holds every PVC, so the ceiling has to be hand-budgeted regardless.
Paying for that with Cluster API, CAPMOX, an IPAM provider, scale-from-zero capacity
annotations, a split node-pool taint scheme, and a permanent extra Kubernetes cluster to run the
controllers in, was a large amount of machinery to arrive back at a number chosen by hand.

Growing the cluster is `count = 4` in OpenTofu plus a node entry in `talconfig.yaml` — a commit
and one job run.

One consequence worth keeping: with no autoscaler draining nodes, the §2.6 argument against
replicated node-local storage (Longhorn, Ceph) rests purely on the capacity multiplier now, not
on scale-down interactions. It is still decisive (D7).

### 6.7 Repair and rebuild

| Situation | Procedure |
|---|---|
| A worker is broken | `talosctl reset` it, or destroy and recreate the VM in OpenTofu, re-apply its config. It rejoins; nothing needs draining beyond the usual. |
| A control-plane node is broken | `talosctl etcd remove-member` first, then recreate. Never re-run `talosctl bootstrap`. |
| etcd quorum lost | Restore from a Talos etcd snapshot (`talosctl etcd snapshot`, taken on a schedule by §6.8 and backed up per §7.7). |
| Total cluster loss | Re-run the §6.2 chain end to end. The runner, the two repos and the age key are the only surviving inputs required. |

Because the same job chain builds the cluster the first time and rebuilds it afterwards, the
rebuild path is exercised every time the cluster is recreated during phases 3–5 — which is the
main reason the throwaway-cluster phase is in the roadmap at all.

### 6.8 Maintenance tasks (cron + manual trigger)

Talos and Kubernetes version upgrades, node image refreshes, **scheduled `talosctl etcd
snapshot` with off-box copy**, certificate expiry checks, ZFS scrubs, Proxmox host updates,
backup restore drills (§7.7). Each should be idempotent, log to one place, and be triggerable
both on a schedule and by a human clicking a button in Semaphore (D6).

### Stage 2 exit criteria
- [ ] A destroyed cluster is rebuilt end to end by one Semaphore job chain, from Git plus the
      age key, with no manual `talosctl` invocations
- [ ] A Talos version bump in `talconfig.yaml` rolls every node without downtime for workloads
      that have a PDB
- [ ] A Kubernetes version bump in the same file completes via `talosctl upgrade-k8s`
- [ ] The cluster comes up with Cilium already running — no post-bootstrap manual CNI install
- [ ] An etcd snapshot has been taken, copied off-box, and restored into a throwaway cluster
- [ ] Cert expiry is monitored and rotation is proven by a forced rollout

---

## 7. Stage 3 — Platform & applications

Flux on the workload cluster, reconciling `stage3/`.

### 7.1 The stage 2 / stage 3 boundary
Explicit rule: **stage 2 owns everything required for the cluster to reach the point where Flux
can run** — CNI and Flux itself, installed as talhelper `inlineManifests` during bootstrap
(§6.2). **Stage 3 owns everything above that line**, including CSI drivers and storage classes.
Without this rule, CNI ownership in particular ends up ambiguous.

Cilium is the awkward case, because it is installed by stage 2 and then *configured* by stage 3.
Resolve it the usual way: stage 2 embeds a minimal Cilium sufficient to make nodes Ready, and
stage 3's `HelmRelease` adopts and owns it from there — same release name and namespace, so the
first reconcile is an upgrade rather than a conflict.

### 7.2 Layout

```
stage3/
  clusters/homelab/          # Flux entrypoint; Kustomizations with dependsOn
  infrastructure/
    controllers/             # Cilium config, cert-manager, external-dns, Proxmox CSI
    configs/                 # ClusterIssuers, Gateways, StorageClasses, static NFS PVs, IPPools
  observability/
  databases/
  apps/
```

Reconciliation order via `dependsOn`: `controllers` → `configs` → `observability` →
`databases` → `apps`.

### 7.3 Ingress — two Gateways

ingress-nginx reached end of life in March 2026 and its intended successor, InGate, was
cancelled before maturity. The Kubernetes project's recommended path is the **Gateway API**, so
this plan uses it rather than Ingress, and the "two ingress classes" requirement becomes two
`Gateway` resources. Implementation is **Cilium's Gateway API** (D8, D9) — no extra component.

| | External Gateway | Internal Gateway |
|---|---|---|
| Reachability | Public, proxied through Cloudflare | LAN / WireGuard only |
| LB IP | `10.100.20.200`, port-forwarded | `10.100.20.201`, never forwarded |
| Certificates | Cloudflare **Origin CA** certs | Let's Encrypt, DNS-01 via Cloudflare |
| Issuer | `cloudflare/origin-ca-issuer` (cert-manager external issuer — automates issuance and renewal, so no manual 15-year cert handling) | cert-manager `ClusterIssuer` with the Cloudflare DNS-01 solver |
| DNS | `external-dns` → Cloudflare, proxied records | `external-dns` → internal zone on `netcore` |

The public zone is already on Cloudflare, so both DNS-01 and the Origin CA path work with one
API token. Split-horizon DNS means the same hostname resolves internally to the internal
Gateway and externally via Cloudflare. Two `external-dns` instances, filtered by annotation or
by Gateway, keep the two record sets separate.

### 7.4 Storage
Per D7 — no in-cluster replication layer, no in-cluster S3:

| Need | Component | Backed by |
|---|---|---|
| Hot RWO block (Postgres, metrics TSDB) | **Proxmox CSI** → `proxmox-ssd` | `ssdpool` |
| Bulk RWO block (media, Loki chunks) | **Proxmox CSI** → `proxmox-hdd` | `hddpool` |
| RWX shared (Nextcloud) | **Static PV/PVC**, no CSI driver | `hddpool/nas` via the `nas` LXC |
| S3 (backups only) | **Backblaze B2** — off-box, §7.7 | — |

### 7.5 Monitoring
**VictoriaMetrics + Grafana + Alertmanager** for metrics, **Loki + Alloy** for logs (D12).
Dashboards and alert rules provisioned as code, not clicked into Grafana. Scrape targets
include the PVE exporter and iDRAC Redfish in VLAN 10 — which is why §4.5 carries explicit
`servers → mgmt` permits.

Keep Loki chunks on `proxmox-hdd` and cap metric retention; **alert on ZFS pool usage at 75%**
so monitoring cannot silently fill the pool it lives on.

### 7.6 PostgreSQL
**CloudNativePG** (D13) — a 3-instance cluster with synchronous replication, anti-affinity
across the `system` pool nodes, rolling minor-version upgrades, and continuous backup to B2
with point-in-time recovery.

Reality check per §2.2: on one host, 3 instances give zero-downtime upgrades and fast failover
from a VM or pod failure, **not** survival of a host failure. The backup destination must be off
this machine or the HA setup is theatre.

### 7.7 Backup & disaster recovery — Backblaze B2

This section spans all three stages — PBS is configured in stage 1, CNPG and Velero in stage 3 —
but it only makes sense as one design. Per §2.2 this is the part that provides actual
resilience: RAID-Z1 survives a disk; B2 survives the chassis, the room, and a ransomware event.

#### What goes where

| Layer | Tool | Route to B2 | Encryption |
|---|---|---|---|
| VM + LXC images | **Proxmox Backup Server 4.2+** | Local datastore on `hddpool` → **sync job** → B2 S3 datastore | PBS client-side |
| PostgreSQL | **CNPG + Barman Cloud plugin** | Direct to B2 S3 endpoint | see caveats |
| PVC data + K8s objects | **Velero + node-agent (kopia)** | Direct to B2 | kopia client-side |
| NAS dataset (`hddpool/nas`) | restic or rclone from the `nas` LXC | Direct to B2 | restic client-side |
| Git repository | GitHub — already off-box | — | — |
| **Secrets root key** | Password manager + offline copy | **never B2** — circular dependency | — |

#### Two-tier PBS — local for speed, B2 for disaster

1. **Local datastore on `hddpool`** (~1 TB budgeted, §2.6) — VMs restore at disk speed rather
   than over the internet. This is the copy you'll actually use, for the overwhelmingly common
   case of "I broke a VM".
2. **Sync job → S3 datastore on B2** — the copy that survives the chassis, the room, and
   ransomware. *S3 Datastore → Sync Jobs → Add*, source location `Local`. Push sync jobs can
   encrypt on the fly before transmission, and the `worker-threads` property parallelises
   groups to raise throughput on a high-latency link.
3. **Chunk cache on the boot NVMe** (~80 GB) — limits S3 API calls. Regenerable, which is why
   it belongs on drives you don't fully trust (§2.3).

PBS gained native S3 datastore support in 4.0 (August 2025) and it **left tech preview in 4.2
(April 2026)**, so this is supported rather than a hack. Deduplication, compression, client-side
encryption, pruning, verification and garbage collection all still apply. Configure the endpoint
under *Configuration → Remotes → S3 Endpoints*.

#### Bucket and key layout

Separate bucket **and** scoped Application Key per consumer — this is about blast radius. A
credential living inside the Kubernetes cluster must not be able to erase your VM backups.

| Bucket | Consumer | Key capabilities |
|---|---|---|
| `homelab-pbs` | Proxmox Backup Server | read/write, no delete where possible |
| `homelab-pgbackup` | CloudNativePG | read/write |
| `homelab-velero` | Velero | read/write |

#### Object Lock — enable it, with one caveat to verify

B2 supports Object Lock (WORM). **Turn it on for at least `homelab-pgbackup` and
`homelab-velero`.** Without it, anything that compromises the cluster can delete the backups
using the very credentials stored there — which is how ransomware turns a recoverable incident
into a total loss.

⚠️ **Verify before relying on it for `homelab-pbs`:** PBS garbage collection needs to delete
unreferenced chunks, and compliance-mode Object Lock prevents exactly that, so GC can fail and
the datastore grows without bound. Test PBS + Object Lock on a throwaway bucket first;
governance mode (which permits privileged deletion) may be the workable compromise.

#### Known B2 + CloudNativePG friction

Documented upstream, and it costs real time if you hit it blind:

- **`HeadBucket` fails against B2** even with valid credentials and permissions
  (cloudnative-pg issue #7105).
- **The `region` key does not populate `AWS_DEFAULT_REGION`** (issue #9724) — set the
  environment variable explicitly.
- Set **`BARMAN_S3_USE_PATH_STYLE=true`** and `AWS_DEFAULT_REGION` in
  `instanceSidecarConfiguration`.
- Endpoint URL takes the form `https://s3.<region>.backblazeb2.com`.
- Build on the **Barman Cloud plugin (CNPG-I)** rather than the older in-tree
  `barmanObjectStore` field, which is on its way out. Check its current status at phase 10.

#### Cost

At roughly **$6–7/TB/month**, a realistic 400–700 GB of deduplicated backups costs about
**$3–5/month**. Egress is free up to 3× average monthly storage, so a full restore is
effectively free — which removes the usual excuse for never testing one.

#### Restore drills

An untested backup is not a backup. Schedule as stage 2 maintenance jobs (§6.8):

- Quarterly: restore a VM from PBS into an isolated network
- Quarterly: PITR a CNPG cluster into a throwaway namespace from B2
- Annually: full rebuild rehearsal — stage 1 Ansible from bare metal, then Git + B2

### Stage 3 exit criteria
- [ ] Both Gateways serve traffic with valid, auto-renewing certificates
- [ ] A new app needs only a directory in `apps/` — no manual DNS or certificate work
- [ ] Dashboards and alerts are in Git and survive a Grafana wipe
- [ ] A Postgres restore from object storage into a fresh cluster has been performed at least once
- [ ] Total cluster loss can be recovered from Git + off-box backups

---

## 8. Repository layout

```
homelab/
├── docs/                      # this plan, runbooks, decision records
├── stage0-network/            # Ansible — manual trigger, workstation only
│   ├── group_vars/all/network.yml   # ← single source of truth for VLANs/subnets/policy
│   ├── roles/                 # opnsense (REST API), comware (template + replace)
│   └── playbooks/             # verify.yml (read-only drift check), apply.yml
├── stage1-bootstrap/          # Ansible
│   ├── inventory/
│   ├── playbooks/
│   └── roles/
├── stage2-cluster/            # applied from the runner LXC (Semaphore)
│   ├── talconfig.yaml         # ← the ONLY place Talos node config is written
│   ├── talenv.yaml            # version vars substituted into talconfig (Renovate target)
│   ├── tofu/                  # VM definitions, bpg/proxmox provider, state backend
│   ├── cilium/                # values rendered into talhelper inlineManifests
│   ├── clusterconfig/         # gitignored — rendered output, never committed
│   └── maintenance/           # scheduled + manual job definitions
└── stage3-platform/           # reconciled by workload-cluster Flux
    ├── clusters/homelab/
    ├── infrastructure/
    ├── observability/
    ├── databases/
    └── apps/
```

### 8.1 Repository visibility — public config, private secrets

| Repo | Visibility | Contains |
|---|---|---|
| `homelab` (this one) | **public** | All stages' manifests, Ansible, docs — everything except secret material |
| `homelab-secrets` | **private** | SOPS+age encrypted material only: Kubernetes `Secret` manifests, and **`talsecret.sops.yaml`** — the Talos secrets bundle (cluster CAs, bootstrap token, etcd PKI) that `talhelper genconfig` needs (§6.1) |

`talsecret.sops.yaml` is consumed by the runner, not by Flux, but belongs here for the same
reason and under the same key. Losing it does not lose the cluster — but it does lose the
ability to add a node or re-render a config, which is most of what stage 2 is for.

Flux consumes both via separate `GitRepository` sources. Coupling is minimal because Kubernetes
Secrets are referenced **by name**, not by path: the private repo delivers `Secret` objects into
namespaces, and the public repo's workloads reference them via `secretRef`. Neither repo needs
to know the other's layout.

The hardware inventory and IP plan stay in the public repo as written — a deliberate choice.
It publishes some reconnaissance value, but everything it describes is behind a firewall.

#### Why not SOPS-encrypted secrets in the public repo

The cryptography is sound — age uses X25519 + ChaCha20-Poly1305 and the ciphertext is opaque.
The problem is the failure modes, all of which get materially worse when the ciphertext is
public:

1. **Git history is permanent, and public is irreversible.** If the age private key ever leaks
   — laptop compromise, a bad backup, an accidental commit — then *every secret ever committed,
   across all history*, becomes decryptable by anyone who cloned or archived the repo. It will
   have been archived (forks, GHArchive, Software Heritage). Rotating afterwards protects the
   future, not the past. With a private repo, **both** the key and repo access have to fail.
2. **The realistic failure is a plaintext commit, not broken crypto.** `.sops.yaml` uses
   path-based creation rules; a new directory matching no rule gets committed unencrypted,
   silently. Public repos are scraped by bots within minutes.
3. **SOPS encrypts values, not keys.** Field names, file structure, namespace names and any
   unencrypted fields remain readable — infrastructure metadata, not noise.

Encrypted **and** private is genuine defence in depth, and costs one extra repository.

#### Guardrails (implement in phase 0, before the repo goes public)

- **Decide before publishing, not after.** A repo that ever contained secrets and is later
  flipped to public exposes its entire history. This repo is currently near-empty, so it is
  clean — keep it that way.
- `.sops.yaml` creation rules with **broad** path regexes, so new directories are covered by
  default rather than by remembering.
- Pre-commit hook (`gitleaks` or equivalent) blocking unencrypted secret material.
- CI check asserting every file matching `*secret*.y*ml` is actually SOPS-encrypted.
- GitHub secret scanning **and** push protection enabled on both repos.
- **Treat any accidental plaintext commit as full compromise** — rotate the credential, don't
  just `git rm` it. History rewriting does not reach forks or archives.

#### Never in Git, in any repo or form

- The **age private key** (password manager + offline copy — see §5.5)
- The **B2 master application key** (use per-bucket scoped keys, §7.7)
- Anything that cannot be rotated

---

## 9. Decision register

All decisions are made. Rationale for the ones where the alternatives were genuinely close
follows the table.

| # | Decision | Choice | Why in one line |
|---|---|---|---|
| D1 | Node OS & bootstrap | **Talos Linux** | Immutable, API-driven upgrades, and no Packer image pipeline to own |
| D2 | Cluster lifecycle engine | **talhelper + OpenTofu (`bpg/proxmox`) + `talosctl`** | The whole cluster is one YAML file and a `tofu` module; no controllers, no CRDs, no alpha APIs |
| D2a | Autoscaling | **None — fixed node pools** | The constraint is disk, which has to be hand-budgeted anyway; autoscaling was a lot of machinery to reach a number chosen by hand (§6.6) |
| D3 | Management-plane placement | **None — stage 2 runs from the `runner` LXC** | With no controllers to host, a whole Kubernetes cluster to hold them is pure overhead (§3) |
| D4 | DHCP + DNS | **OPNsense keeps DHCP + resolver; PowerDNS in `netcore` for `internal.<domain>` only** | Household infrastructure must not live on the box this project rebuilds; `external-dns` needs dynamic updates OPNsense can't do |
| D5 | Stage 1 + 2 job runner UI | **Semaphore UI** | Single Go binary, runs Ansible *and* OpenTofu, fits an LXC, works when Kubernetes doesn't — and under D2 it is the cluster's lifecycle engine, not a convenience |
| D6 | Day-2 ops UI | **Semaphore + a Flux UI** (Capacitor / Headlamp) | Semaphore runs things, the Flux UI observes and forces reconciliation. Add Argo Workflows later only if maintenance grows genuinely multi-step |
| D7 | Storage | **Proxmox CSI (tiered) + static NFS RWX, no in-cluster S3** | ZFS already gives redundancy and snapshots; replication on top would cost 2–3× of the scarcest resource |
| D8 | CNI | **Cilium** | Folds LB-IPAM and Gateway API into the CNI, removing two components |
| D9 | Gateway implementation | **Cilium Gateway API** | Follows from D8 — no extra component, eBPF data path. ingress-nginx is EOL (§7.3) |
| D10 | LB IP assignment | **Cilium LB-IPAM**, L2 announcements, VIPs `10.100.20.200–.250`; **Talos VIP** for the control plane | Built into D8; L2/ARP requires the VIP in the node subnet, which is why there is no VIP VLAN |
| D11 | Secrets management | **SOPS + age, secrets in a private repo** | Native Flux decryption, no extra infrastructure; privacy adds defence in depth (§8.1) |
| D12 | Monitoring | **VictoriaMetrics + Grafana + Alertmanager; Loki + Alloy** | Wins on *disk* footprint — the constrained resource — while accepting kube-prometheus-stack's dashboards and rules |
| D13 | PostgreSQL operator | **CloudNativePG** | De facto standard; built-in continuous backup and PITR to object storage. Not close |
| D14 | Backup & DR | **PBS + Velero + CNPG-native, all → Backblaze B2** | Layers, not alternatives; the only thing providing real resilience (§7.7) |
| D15 | Repository strategy | **Public config monorepo + private secrets repo** | Atomic cross-stage changes; the split is about visibility, not stages (§8.1) |
| D16 | Object storage | **Backblaze B2 only, no in-cluster S3** | Backups must not live on the machine they protect; nothing else currently needs an S3 API |

### D7 — why not the alternatives

- **Rook-Ceph.** Guidance is not to run it under 32 GB RAM per node; replication across OSDs on
  one box costs 3× IO and capacity for no protection against disk or host loss. Worse, its OSDs
  would sit on zvols on top of ZFS — copy-on-write on copy-on-write, with bad write
  amplification. A clear no. **Revisit seriously if a second and third physical node appear** —
  Proxmox CSI does not close that door.
- **Longhorn.** The usual homelab default, and it would work, but it is another replication
  layer on top of ZFS you don't need, at 2–3× of the scarcest resource (§2.6), and its
  NFS-based RWX is a performance and failure-mode compromise next to a plain export from the
  `nas` LXC.
- **democratic-csi over NFS/iSCSI.** Leans on ZFS directly and is mature, but needs a NAS VM or
  host-side export in the data path for what static PVs already achieve here.
- **SeaweedFS / JuiceFS.** Closest to one system for everything, but PVCs are FUSE mounts, so
  databases still want real block storage underneath.
- **In-cluster S3 (Garage/MinIO).** Loki was the only "hot" S3 consumer, and a filesystem-backed
  PVC on `proxmox-hdd` serves it at this scale. Garage is a cheap add-back if a future app
  genuinely only speaks S3 — a door not walked through, not one closed. MinIO is no longer a
  sensible default regardless: the upstream open-source repo was archived on 2026-02-12, the
  OpenMaxIO fork is dormant, and only `pgsty/minio` is backporting patches.

> **Object storage cannot be the *only* storage layer.** S3 serves an HTTP API, not block
> devices or POSIX filesystems, so it cannot back a PVC for Postgres, etcd or Prometheus.
> S3-backed CSI drivers (`csi-s3`, Mountpoint-S3, geesefs) mount buckets over FUSE with no real
> file locking, non-atomic renames and poor random-write performance — acceptable for media and
> write-once blobs, unsafe for databases. Object storage here is a **backup and bulk-data
> target**, layered on top of real block storage, never a replacement for it.

### D2 — what was given up, and why it was worth it

The alternative was Cluster API with the Proxmox provider (CAPMOX), a Talos bootstrap provider,
an IPAM provider and a ClusterClass. It is the more impressive architecture and it buys two
genuine things: continuous reconciliation (a deleted node comes back on its own) and
declarative autoscaling.

Neither survives contact with this hardware. Autoscaling is ruled out on its own merits (D2a).
And self-healing is worth much less on a single host than it sounds: the failure modes it
covers — a node VM lost, a machine template drifting — are ones where the host is still up and
a job run fixes it in minutes, while the failure mode that actually ends the day is the host
itself, which no in-cluster controller helps with.

Against that: CAPMOX is a modest project still on `v1alpha2` with `v1alpha3` in flight, so
breaking API changes were an expected cost, on top of CAPI core's own upgrade cadence, and all
of it needed a permanent extra Kubernetes cluster to run in (D3). talhelper's failure mode by
comparison is a CLI that renders YAML: if the project stalled tomorrow, the rendered machine
configs are plain Talos config and still apply.

**Revisit if a second and third physical node appear.** At that point self-healing starts
covering real failures rather than convenient ones, and the fixed cost of a management cluster
is amortised across an estate rather than carried by one box.

### D11 — the upgrade path

**External Secrets Operator** remains the upgrade path if rotation and audit ever matter. It
becomes *more* attractive now the config repo is public, since `ExternalSecret` manifests carry
only references and are safe to publish. The cost is a service to run and back up, plus a
bootstrap dependency.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Stage 0 change locks you out of the switch** | Total loss of remote access — VLAN 10 carries iDRAC, the Proxmox host *and* the switch's own management IP, so every remote path dies together | Arm the SAFETY-NET scheduled reboot before every apply (§4.8); apply VLAN 10 changes last; keep a tested USB-serial console; never run stage 0 from the `runner` LXC |
| Missing `servers → mgmt` ACL permits | `external-dns` silently can't write records; monitoring silently loses the PVE and iDRAC targets | The four permits are enumerated in §4.5 and generated from `network.yml`; assert them in `verify.yml` |
| No stateful inspection or IDS between VLANs | East-west traffic is filtered only by stateless ACLs; a compromised client has unfiltered reach into mgmt and servers, since VLAN 40 is a trusted tier (§4.5) | Accepted — the 10 Gb requirement rules out router-on-a-stick (§4.1), and trusting the client VLAN is a deliberate choice for usability. The blast radius shrinks once IoT/guest devices leave VLAN 40 (§4.8) |
| Wireless behind the unmanaged switch | Every phone and smart plug shares the trusted VLAN 40 with the workstation, so an untrusted device reaches mgmt unfiltered | Accepted for now; §4.8 option 3 (APs on the 5130) is the exit, and is cheapest if cable is being run anyway |
| OPNsense outbound NAT / static route missing after a rebuild | Every VLAN silently has no internet; looks like a switch fault | Both are in `stage0-network` and asserted by `verify.yml` (§4.4, §4.7) |
| **ZFS pool fills up** | Writes fail cluster-wide; severe performance degradation past 80% | The main capacity risk on this hardware. Alert at 75%; cap metrics/Loki retention; node count is fixed and budgeted (§2.6, §6.6); add disks via `zpool attach` (§2.5) |
| 8K `volblocksize` left in place on RAID-Z1 | ~166% space usage — hundreds of GB wasted | Verify and set ≥16K in stage 1 (§5.1); migrate any existing volumes |
| Adding nodes ad hoc without re-checking the budget | Pool fills; new VMs fail to start | Node count and root-disk size are explicit in `talconfig.yaml` and §2.6; alert on pool usage at 75%, not just memory |
| Postgres write amplification on RAID-Z1 | Poor DB performance | Assessed negligible at ~30 tps (§2.7); revisit with a mirror vdev only if load grows materially |
| Second disk failure during an `hddpool` resilver | Pool lost, restore from B2 required | Accepted: drives are of differing ages (uncorrelated wear) and B2 covers the data regardless — the cost is restore time, not data |
| PBS GC fails under compliance-mode Object Lock | `homelab-pbs` grows without bound | Test on a throwaway bucket before relying on it; governance mode is the likely compromise (§7.7) |
| Single host failure | Total outage | Accepted by design — off-box backups (D14) and a tested rebuild path are the answer |
| **No controller repairs a failed node** | A dead node stays dead until someone notices | Accepted, deliberately (§3, D2). Alert on `NotReady` nodes so "notices" is not a person spotting it; the repair is a job run, not a runbook (§6.7) |
| `runner` LXC lost | Nothing can build or repair the cluster | It is a 10 GB LXC rebuilt by stage 1 Ansible, backed up by PBS, holding no unique state except OpenTofu state — which must be backed up or kept in the private repo |
| `talsecret.sops.yaml` lost | Cannot render configs, so cannot add or re-provision a node | Private repo (§8.1) plus the age key backup; the running cluster survives, but stage 2 does not |
| Secret root key loss | Cannot decrypt anything in Git, **including the Talos secrets bundle** | Password manager + offline copy; document the recovery procedure |
| Talos learning curve | Slow early progress | Build a throwaway cluster first; keep `talosctl` access documented |
| OpenTofu state corruption or loss | `tofu` no longer knows which VMs it owns; risk of duplicate or orphaned VMs | Keep state on the runner with a backup to the private repo or B2; `tofu import` is the recovery path, and with 6 VMs it is tedious rather than fatal |
| Gateway API learning curve | Slow stage 3 | Start with one Gateway and one HTTPRoute; migrate incrementally |

---

## 11. Implementation roadmap

Each phase should end with something demonstrably working.

| Phase | Deliverable |
|---|---|
| **0** | Scaffold the repo; create the private `homelab-secrets` repo; generate the age keypair and back it up; install the §8.1 guardrails (`.sops.yaml`, pre-commit hook, CI check, push protection) **before** making this repo public |
| **0a** | **Stage 0 by hand, from the console.** Confirm the 5130's feature set and the PERC's HBA mode (§2.1); serial console tested; VLANs + SVIs + transit /30; OPNsense static route, outbound NAT and DHCP relay subnets; Proxmox trunk with PVID 10. *Exit: every VLAN routes, the PC sustains ≥5 Gbit/s to a VM in VLAN 20, and OPNsense's Git config backup is on.* |
| **0b** | **Stage 0 as code.** `network.yml`, the Comware template, the OPNsense role, `verify.yml`/`apply.yml`, WireGuard, SAFETY-NET rollback proven by deliberately breaking a config. *Exit: `verify.yml` reports zero drift, and ACLs enforce the trust hierarchy (§4.5).* Do this only once 0a is stable — you want a known-good config to render *toward*. |
| **1** | Stage 1 Ansible: Proxmox host config, `pvecm create`, **create `hddpool`**, verify `volblocksize`, VLAN-aware `vmbr0`, `netcore` LXC. *Exit: internal DNS zone resolves and accepts a dynamic update; both pools present; host reproducible.* |
| **2** | Stage 1 continued: `runner` LXC + Semaphore + the pinned stage 2 toolchain (§5.3), `nas` LXC (SMB + NFS), secrets root. *Exit: a job runs from the UI; SMB share mounts from a desktop; `talhelper` renders from an encrypted secret file.* |
| **2b** | **PBS: local datastore on `hddpool` + B2 S3 datastore + sync job.** *Exit: a VM backup exists locally and in B2, and a test restore succeeds.* Done early deliberately — everything after this is recoverable. |
| **3** | Stage 2: the OpenTofu module and a **throwaway single-node** Talos cluster, applied by hand from the runner. *Exit: `talosctl` reaches a booted node and `kubectl get nodes` works.* |
| **4** | Stage 2: full `talconfig.yaml` — 3 control plane + 3 workers, static addressing, Talos VIP, Cilium as an inlineManifest. *Exit: `kubectl get nodes` shows six Ready nodes with no manual CNI install.* |
| **5** | Stage 2: wrap the §6.2 sequence as Semaphore jobs; prove upgrades and rebuild. *Exit: destroying the cluster and re-running one job chain rebuilds it; a `talosVersion` bump rolls every node.* |
| **6** | Stage 2: `flux bootstrap` + `sops-age` Secret, etcd snapshot job. *Exit: Flux reconciles `stage3-platform/`; a snapshot restores into a throwaway cluster.* |
| **7** | Stage 3: Cilium `HelmRelease` adopting the bootstrap install (§7.1), cert-manager, both Gateways, external-dns. *Exit: a test app is reachable internally and externally with valid certs.* |
| **8** | Stage 3: Proxmox CSI (`proxmox-ssd`, `proxmox-hdd`) + static NFS PVs (`nfs-nas`). *Exit: PVCs provision on both classes and snapshot; the NFS PV mounts and is writable from a pod.* |
| **9** | Stage 3: monitoring, dashboards, alerting — including **ZFS pool usage alerts at 75%** and iDRAC hardware health. *Exit: alerts reach you.* |
| **10** | Stage 3: CloudNativePG + B2 backups (mind the §7.7 B2 quirks) + Nextcloud on `nfs-nas`. *Exit: a PITR restore into a fresh cluster succeeds.* |
| **11** | Maintenance automation, cron jobs, backup/restore drills, runbooks. *Exit: a full rebuild from Git + backups is documented and tested.* |

Phases 3–5 deliberately use throwaway clusters — expect to destroy and recreate several times
while `talconfig.yaml` settles. That repetition is the point: it is the same job chain that
constitutes the disaster-recovery path (§6.7), so exercising it here is what makes it
trustworthy later. Don't put real data on the cluster until phase 8.

---

## 12. Things to verify along the way

Not open decisions — items that must be confirmed against real firmware, real hardware or a
moving upstream before they are relied upon.

| # | Verify | When |
|---|---|---|
| 1 | The 5130 is the **EI** feature set (`display version`) — SVI IP addressing, `packet-filter`, `dhcp select relay` (§2.1) | Phase 0a, before anything |
| 2 | `configuration replace file` exists on the installed Comware firmware (§4.7) | Phase 0b |
| 3 | SAFETY-NET `scheduler` syntax matches the firmware (§4.8) | Phase 0b |
| 4 | `ansibleguy.opnsense` covers Kea, not just legacy ISC dhcpd (§4.7) | Phase 0b |
| 5 | The PERC exposes all four SAS drives as raw non-RAID devices (§2.1) | Phase 1 |
| 6 | `volblocksize` on the existing `ssdpool` storage is 16K, not 8K (§2.2, §5.1) | Phase 1 |
| 7 | PBS garbage collection survives B2 Object Lock — test on a throwaway bucket (§7.7) | Phase 2b |
| 8 | The Image Factory schematic includes `siderolabs/qemu-guest-agent`, and Proxmox actually reports the guest IP (§6.3) | Phase 3 |
| 9 | `bpg/proxmox` handles the Talos boot flow you choose — ISO vs imported `nocloud` disk — without needing cloud-init that Talos ignores (§6.1) | Phase 3 |
| 10 | Cilium as a talhelper `inlineManifest` with KubePrism at `localhost:7445` brings nodes Ready without a manual helm install (§6.2) | Phase 4 |
| 11 | Barman Cloud plugin (CNPG-I) status, vs the deprecated in-tree `barmanObjectStore` (§7.7) | Phase 10 |
