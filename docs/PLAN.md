# Homelab GitOps Automation — Plan

> Status: **all Tier-1 decisions made.** Section 9 is the decision register; Tier-2 items
> are decided at the relevant stage.
>
> **2026-08-17 — networking reworked.** A new **stage 0** (§4) owns VLANs, inter-VLAN routing
> and firewall policy; the old §5.7 draft is superseded. Key change: the **HPE 5130 does the
> inter-VLAN routing**, not OPNsense, which is what preserves the 10 Gb server↔PC path. This
> resolves **D4** and constrains **D10**.
>
> Last updated: 2026-08-17

---

## 1. Goal

Automate a single-server homelab, from bare Proxmox VE to running workloads, in four
stages that each have a clear boundary and a clear "break glass" story.

| Stage | Owns | Driven by | Lives where |
|---|---|---|---|
| **0 — Network** | VLANs, inter-VLAN routing and ACLs, DHCP/resolver, the router↔switch boundary | Ansible, run from a workstation, **manually triggered** | OPNsense + HPE 5130 |
| **1 — Bootstrap** | Proxmox host config, internal DNS, secrets root, job-runner UI, management cluster | Ansible, run from a workstation | Proxmox host + LXCs + 1 VM |
| **2 — Cluster lifecycle** | Creating/upgrading/repairing the workload Kubernetes cluster and its side infrastructure | GitOps (Flux on the management cluster) | Management cluster |
| **3 — Platform & apps** | Ingress, certs, storage classes, monitoring, Postgres, apps | GitOps (Flux on the workload cluster) | Workload cluster |

### Design principles

1. **Each stage can rebuild the one above it.** Stage 1 survives the total loss of the
   Kubernetes clusters and can recreate them; stage 0 survives the loss of everything else.
   This is the single most important property and it drives the placement decisions below.
2. **Stages 0 and 1 are not GitOps.** They are the chicken-and-egg layers, so they are plain
   idempotent Ansible run from a laptop. They are still fully in Git. Stage 0 goes further and
   is **never** reconciled automatically: a controller that manages the network it depends on
   cannot roll back its own mistakes (§4).
3. **Declarative over imperative wherever a controller exists.** Upgrades should be a
   version bump in a YAML file, not a runbook.
4. **Assume the host dies.** See §2 — nothing in this design provides hardware fault
   tolerance, so off-box backups are the real disaster-recovery story, not replication.

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
| HDD tier | 4× SAS HDD, 1.8 TB each — **unconfigured**, ~5.4 TB usable as RAID-Z1 |

### 2.2 What this means

**RAM and CPU are effectively free. Fast storage is the binding constraint; bulk storage is
not.**

With 512 GB you can run as many control-plane and worker VMs as you like, give the management
cluster a comfortable allocation, and stop worrying about the memory footprint of monitoring.
The SAS drives remove the capacity crunch for bulk data, but they are **spinning disks** — they
do not relieve pressure on the 1.8 TB SSD tier, which is where anything latency-sensitive has
to live. See §2.3 for the tiering and §2.6 for the budget.

Consequences, in order of importance:

- **RAID-Z1 already provides disk redundancy on both tiers.** One drive per pool can fail
  without data loss. Any replication you add at the Kubernetes layer (Longhorn, Ceph) is
  therefore **not buying protection from disk failure** — it is buying protection from VM loss
  and logical corruption, at a 2–3× multiplier. On the SSD tier that trade is not worth it.
  This drives D7 hard.
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
  PostgreSQL are relentlessly sync-write heavy — putting them there would be the worst
  placement available despite the drives being nominally the fastest. It's also LVM, not ZFS:
  no checksums, no snapshots, no `send`/`recv`. **Use it only for regenerable data** (§2.3).
- **Autoscaling is bounded by SSD-tier disk, not RAM.** Each burst node needs a root disk. With
  512 GB RAM the autoscaler will never run out of memory; it can absolutely fill `ssdpool`. §6.4.
- **Keep every pool under ~80% full.** ZFS performance and fragmentation degrade badly past
  that, so working budgets are **~1.44 TB** (SSD) and **~4.32 TB** (HDD).
- **Dual-socket NUMA.** Large VMs should either fit within one NUMA node or have NUMA topology
  exposed in Proxmox, or you'll pay a memory-latency penalty. Relevant for the bigger workers.
- **The Proxmox host is the largest SPOF and is not managed by stages 2 or 3.** That's why
  stage 1 configures it as code rather than leaving it hand-tuned.

**Corollary — the backup rule:** at least one copy of anything you care about (Postgres
backups, PVC snapshots, the Git repo, the secrets root key) must live **off this machine**.
RAID-Z1 is not a backup. See D14.

### 2.3 Storage architecture — three tiers, host-managed

**Decided (2026-08-16): all storage stays under Proxmox's ZFS. No TrueNAS VM.** Rationale in
§2.4.

| Tier | Devices | Layout | Usable | Role |
|---|---|---|---|---|
| **boot** | 2× consumer NVMe | RAID1 + LVM | ~150 GB free | Proxmox OS, ISOs, CT templates, **PBS chunk cache** — regenerable data *only* |
| **`ssdpool`** | 3× SATA SSD | ZFS RAID-Z1 *(existing)* | 1.8 TB | VM disks, etcd, PostgreSQL, hot RWO PVs |
| **`hddpool`** | 4× SAS HDD | ZFS RAID-Z1 *(to create)* | ~5.4 TB | NAS datasets, bulk/cold PVs, NFS RWX exports, **PBS local datastore** |

This maps onto Kubernetes as three StorageClasses, which is the neat part — Proxmox CSI
exposes each Proxmox storage separately, so tiering costs nothing extra:

| StorageClass | Backed by | Access | For |
|---|---|---|---|
| `proxmox-ssd` | `ssdpool` | RWO | PostgreSQL, metrics TSDB, anything latency-sensitive |
| `proxmox-hdd` | `hddpool` | RWO | Media, Loki chunks, bulk app data |

RWX volumes (`nfs-nas`) aren't a StorageClass at all — no CSI driver, no dynamic
provisioning. For the handful of shared volumes a homelab actually needs (Nextcloud, maybe
one media share), each is a **static PV/PVC pair** defined in Git, pointing directly at
`hddpool/nas` over NFS. One fewer controller running in the cluster, at the cost of writing
a PV by hand instead of just requesting a PVC.

#### Why the boot NVMe should not hold VM disks

You suggested this, and it's a reasonable instinct — it's the fastest device. Three reasons
not to:

1. **It doesn't fit.** VM disks budget to ~370 GB (§2.6); you have ~150 GB.
2. **Consumer NVMe without PLP is the wrong device for etcd.** Sync-write latency is poor
   precisely where etcd and Postgres hurt most, and endurance is low. Nominally fastest,
   actually worst for this workload.
3. **It's LVM, not ZFS** — no checksums, no snapshots, no `send`/`recv`, inconsistent with
   everything else.

Its best use is **regenerable data**: ISOs, container templates, and the PBS chunk cache. If
those drives die you lose a cache, not data — which is exactly what you want from storage you
don't fully trust. Monitor wear anyway.

#### The NAS, and why it also solves RWX

A single dataset serves both needs, which is the tidiest part of this design:

- `hddpool/nas` is exported by a **lightweight `nas` LXC running Samba + NFS**, configured by
  stage 1 Ansible. Being outside Kubernetes, **the file shares keep working when the cluster is
  down** — which matters for a household NAS.
- The **same NFS export** backs the `nfs-nas` static RWX volumes. This answers D7's outstanding
  RWX question with no additional moving parts — not even a CSI driver.
- "Cloud storage" (sync clients, sharing, mobile apps) is **Nextcloud in the cluster** on a
  static `nfs-nas` RWX volume pointed at that dataset — so SMB users and cluster workloads see
  the *same files*, rather than two copies to reconcile.

### 2.4 Why not TrueNAS (and when it would be right)

TrueNAS SCALE is a genuinely good NAS product, and "TrueNAS VM owns the disks, serves PVs over
NFS/iSCSI via democratic-csi" is a well-trodden pattern. It's ruled out here by one hard
constraint plus three softer ones.

**The hard one — you cannot pass through only the HDDs.** PCIe passthrough works at
IOMMU-group granularity: you pass the *entire* controller, and every disk attached to it goes
with it, becoming unavailable to the host. Your SATA SSDs and SAS HDDs are almost certainly on
the same PERC, so passing it to TrueNAS would take `ssdpool` — and therefore every VM disk —
away from Proxmox. Passing individual disks instead (`qm set -scsiN /dev/disk/by-id/...`) is
explicitly discouraged: TrueNAS loses SMART access and direct device control, and it's a known
route to pool corruption. **This only becomes viable if you add a second, separate HBA.**

The softer ones, which would still apply even with a second controller:

- **A second ZFS layer with its own ARC**, duplicating what the host already does well.
- **A SPOF in the PV data path.** Every persistent volume in the cluster goes offline while the
  NAS VM reboots for updates. Host-managed ZFS has no such window.
- **It isn't GitOps.** TrueNAS configuration is UI-driven; it would be the one part of the
  estate not described by this repo.

What you'd give up by not running it: a polished web UI for shares, snapshot browsing, and user
management. §2.3's `nas` LXC covers the function; it doesn't match the presentation. If the UI
matters a lot to you, say so — it's a legitimate reason to revisit, and adding an HBA is cheap.

### 2.5 Why `ssdpool` stays a single 3-disk pool

A dedicated mirror vdev for PostgreSQL (2 extra SSDs, matched 8K `volblocksize`, no RAID-Z
parity padding or read-modify-write cost) would be the clean fix for the tension in §2.7 — but
it isn't worth it here. Actual load is on the order of **~30 transactions/second**, well within
what a single SSD absorbs even with RAID-Z1's write-amplification tax included, and 512 GB of
RAM already gives a large ZFS ARC and generous `shared_buffers` to absorb the read side. Buying
2–3 drives to fix a cost you can't measure isn't a good trade.

`ssdpool` therefore stays the existing 3× SATA SSD RAID-Z1, hosting VM disks, etcd and
PostgreSQL together — no dedicated pool, no mirror vdev.

If load ever grows enough to revisit this: `zpool attach` (RAIDZ expansion, OpenZFS 2.3+, in
Proxmox VE 9) can grow `ssdpool` live, one disk at a time, without a rebuild — but only within
the same RAID-Z1 parity scheme. It adds capacity, not the fix; the block-size tension is only
actually removed by a mirror, which needs a fresh pool built from scratch, not an expansion.

### 2.6 Capacity budget (draft)

Talos keeps node disks small, which helps considerably on the SSD tier.

**Boot NVMe — ~150 GB free, regenerable data only**

| Item | Total |
|---|---|
| ISOs, container templates, Talos images | ~40 GB |
| PBS chunk cache (§7.7) | ~100 GB |

**`ssdpool` — 1.8 TB raw, ~1.44 TB working budget**

| Item | Count | Each | Total |
|---|---|---|---|
| Stage 1 LXCs (`netcore`, `runner`, `nas`) | 3 | 10 GB | 30 GB |
| Management cluster VM | 1 | 40 GB | 40 GB |
| Control plane (Talos) | 3 | 20 GB | 60 GB |
| System pool workers | 3 | 40 GB | 120 GB |
| Burst pool workers (at max) | 0–6 | 20 GB | up to 120 GB |
| **VM subtotal** | | | **~370 GB** |
| **Remaining for hot RWO PVs (`proxmox-ssd`)** | | | **~1.07 TB** |

**`hddpool` — 5.4 TB raw, ~4.32 TB working budget**

| Item | Total |
|---|---|
| PBS local datastore (§7.7) | ~1.0 TB |
| NAS datasets (`hddpool/nas`, SMB + `nfs-nas` RWX) | ~1.2 TB |
| Bulk/cold RWO PVs (`proxmox-hdd`) — media, Loki chunks | ~0.6 TB |

The SAS tier changes the picture substantially: bulk capacity is no longer scarce, and the
SSD tier now carries only VMs and latency-sensitive volumes.

Replication multipliers still decide D7 on the **SSD tier**, where headroom is finite:

| Storage choice | Multiplier | Usable hot application data |
|---|---|---|
| **Proxmox CSI** (ZFS-backed, no extra replication) | 1× | **~1.07 TB** |
| Longhorn, 2 replicas | 2× | ~535 GB |
| Longhorn, 3 replicas (default) | 3× | ~357 GB |
| Rook-Ceph, 3× replication | 3× | ~357 GB, *and* Ceph OSDs on zvols means CoW-on-CoW |

RAM was never going to decide this. Disk does, and decisively.

### 2.7 The PostgreSQL block-size tension — accepted at this workload

Worth stating explicitly because it affects D13's deployment, not just D7:

- Postgres writes 8K pages.
- 3-disk RAID-Z1 wants ≥16K `volblocksize` to avoid padding waste.
- 16K blocks + 8K writes = read-modify-write amplification on every page write.

**Decided: accept it, on 16K `volblocksize`.** The theoretical fix — a dedicated mirror vdev
with matched 8K `volblocksize` (§2.5) — has no RMW cost at all, but isn't worth the extra
drives here: at the actual expected load (~30 transactions/second), even doubled write cost
from RMW amplification is nowhere near what a single SSD can absorb, and 512 GB of RAM gives
both a large ZFS ARC and generous `shared_buffers`, which covers most of the read side anyway.

`full_page_writes=off` (safe specifically because ZFS's own writes are atomic) is available as
a further tuning knob if this ever needs revisiting, but isn't necessary at the current scale.

### 2.8 Remaining hardware questions

- ~~NIC(s): speed and count, and whether the switch/router does VLANs~~ — answered, §4.1:
  10 G SFP+ to an HPE 5130 doing VLANs and L3, plus 1 G RJ45 for iDRAC
- ~~Existing router/firewall: make/model, and whether it already runs DHCP/DNS~~ — answered,
  §4.1/§4.6: OPNsense, and it keeps both roles
- **Confirm the 5130 is the EI feature set** (JG937A should be a 5130-48G-PoE+-4SFP+ EI). Stage
  0 needs `interface Vlan-interface N` to accept an `ip address`, plus `packet-filter`,
  `dhcp select relay` and ideally `configuration replace file`. The SI variants are more
  limited. Check with `display version` / `display device manuinfo`.
- Does Proxmox boot from the 3 SSDs, or from a BOSS/M.2 card?
- Is the PERC controller in HBA/IT (non-RAID) mode? ZFS requires it — presumably yes if
  RAID-Z1 is genuinely ZFS, but worth confirming.
- Free drive bays: how many? Confirms there's room for the 4th SAS HDD.

---

## 3. Target architecture

```
  ── STAGE 0 ──────────────────────────────────────────────────────────
   OPNsense ──1G transit, VLAN 99──► HPE 5130 (L3: SVIs v10/v20/v40)
   NAT + north-south firewall        east-west routing + ACLs, wire speed
   DHCP server + resolver            10G SFP+ ─► Proxmox (trunk, PVID 10)
                                     10G SFP+ ─► living room (access, v40)
┌─────────────────────────────────────────────────────────────────────┐
│ Proxmox VE host (single node)          vmbr0 vlan-aware, VLAN 10/20  │
│ configured by Stage 1 Ansible: ZFS, bridges/VLANs, firewall, API    │
│ tokens, backup jobs                                                 │
│                                                                     │
│  storage tiers:  boot NVMe (ISOs, PBS cache)                        │
│                  ssdpool  1.8 TB  → VM disks, hot PVs               │
│                  hddpool  5.4 TB  → NAS, bulk PVs, PBS local        │
│                                                                     │
│  ── STAGE 1 ─────────────────────────────────────────────────────   │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                 │
│  │ LXC: netcore │ │ LXC: runner  │ │ LXC: nas     │                 │
│  │ authoritative│ │ web UI, runs │ │ SMB + NFS on │                 │
│  │ internal DNS │ │ Ansible /    │ │ hddpool/nas  │                 │
│  │ zone (v10)   │ │ OpenTofu(v10)│ │  (VLAN 20)   │                 │
│  └──────────────┘ └──────────────┘ └──────┬───────┘                 │
│  ┌────────────────────────┐               │                         │
│  │ VM: mgmt cluster       │               │ NFS → nfs-nas (RWX)     │
│  │ single-node k8s + Flux │               │                         │
│  └───────────┬────────────┘               │                         │
│              │                            │                         │
│  ── STAGE 2 (GitOps, reconciled by mgmt Flux) ────────────────────   │
│   Cluster API + Proxmox provider + IPAM   │                         │
│   cluster-autoscaler                      │  creates / upgrades     │
│   maintenance jobs (certs, image builds)  │  / repairs              │
│              ▼                            │                         │
│  ┌───────────────────────────────────────────────────────────┐      │
│  │ Workload Kubernetes cluster (VMs)      ◄──┘                │      │
│  │  control plane ×3  │ system pool (fixed) │ burst pool      │      │
│  │                    │ storage, DBs        │ (autoscaled)    │      │
│  │  ── STAGE 3 (GitOps, reconciled by workload Flux) ───────  │      │
│  │  CNI · CSI (ssd/hdd) · Gateway ×2 · cert-manager           │      │
│  │  external-dns · monitoring · CloudNativePG · Nextcloud     │      │
│  └───────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────┘
        │ PBS local datastore (hddpool) ──sync──► Backblaze B2
        │ CNPG / Velero / restic ─────────────► Backblaze B2
```

### Why a separate management cluster

Cluster API needs somewhere to run that is **not** the cluster it manages. The alternatives
are covered in D3, but the recommended shape is a small single-node cluster created by
stage 1. It gives stage 2 a natural home, makes "stage 2 is GitOps" true rather than
aspirational, and means a totally destroyed workload cluster can be rebuilt by a controller
that is still running rather than by a human with a runbook.

---

## 4. Stage 0 — Network foundation

**Tool:** Ansible, run from your **workstation only**, triggered by hand.
**Property:** every layer above depends on it, and nothing above it can repair it.

Stage 0 was originally folded into stage 1 as a "network plan" subsection. It has been split out
because it is genuinely a layer *below* the bootstrap: stage 1 Ansible reaches the Proxmox
host over this network, so the network cannot be one of the things stage 1 configures.

> **Stage 0 is never GitOps.** A reconciler that manages the network it depends on is a
> circular dependency with an unrecoverable failure mode: push a bad ACL, lose contact with
> the switch, and the controller that would roll it back can no longer reach it. Cluster API
> rebuilding a VM is recoverable; Flux bricking the default gateway is not. Stage 0 is
> human-triggered, always.
>
> For the same reason, **stage 0 must not run from the `runner` LXC** (§5.3) — the runner sits
> behind the switch it would be reconfiguring, on a host whose management VLAN it might be
> changing.

### 4.1 The hardware, and the one decision that shapes everything

| Device | Role | Links |
|---|---|---|
| OPNsense | WAN termination, NAT, north-south firewall, DHCP server, resolver | 4× 1 GbE — 1 WAN, 1 LAN, 2 spare |
| HPE 5130 (JG937A) | Core L2/L3 switch | 48× 1 GbE + 4× SFP+ 10 G |
| Living-room switch | **unmanaged**, 10 G SFP+ uplink | PC and household devices |
| R640 | Proxmox host | 10 G SFP+ (data) + 1 G RJ45 (iDRAC) |

The requirement was "separate the network infrastructure, the server VMs and the client
devices, **but keep a 10 Gb path between the server and the PC**". Those two pull in opposite
directions if the router is the separation point: the router is on a 1 Gb link, so any
inter-VLAN traffic that goes through it is capped at 1 Gb.

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
forwards 2–5 Gbps per flow, so PC↔NAS transfers would be capped by router CPU instead of by
link speed. The switch ASIC is free.

**This also removes the original reason for a separate DHCP server.** That requirement came
from the assumption that separation had to come from the router. It doesn't — see §4.6.

```
                       ISP
                        │
                 ┌──────┴───────┐
                 │  OPNsense    │  WAN: DHCP from ISP        2 spare 1G ports
                 │  10.100.99.1 │  NAT · north-south FW · DHCP server · resolver
                 └──────┬───────┘
                        │ 1 GbE — transit ONLY, VLAN 99, 10.100.99.0/30
                 ┌──────┴──────────────────────────────┐
                 │  HPE 5130 EI          10.100.99.2   │
                 │  SVIs: v10 v20 v40 (v50 later)      │ ← east-west routing
                 │  ip route-static 0.0.0.0 0 …99.1    │   + packet-filter ACLs
                 └───┬─────────────────────────┬───────┘
         10G SFP+    │                         │  10G SFP+ access, VLAN 40
            trunk    │                         │
       ┌─────────────┴────────┐      ┌─────────┴──────────┐
       │ R640 — Proxmox       │      │ living-room switch │
       │ vmbr0 (vlan-aware)   │      │  (unmanaged)       │
       │  PVID 10: host, LXCs │      │  PC, TV, phones…   │
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

| VLAN | Subnet | Gateway (SVI) | Holds |
|---|---|---|---|
| 1 | — | *none* | **Quarantine.** No SVI, no route. All unused ports park here. |
| 10 | `10.100.10.0/24` | `.10.2` | Infra: switch `.2`, iDRAC `.40`, PVE host `.41`, `netcore` + `runner` LXCs, mgmt cluster VM |
| 20 | `10.100.20.0/24` | `.20.2` | Server workloads: Kubernetes nodes, `nas` LXC, **LB VIPs `.200–.250`** |
| 40 | `10.100.40.0/24` | `.40.2` | Clients: PC, laptops, phones, TV — everything behind the living-room switch |
| 50 | `10.100.50.0/24` | `.50.2` | IoT / guest — **deferred**, see §4.8 |
| 99 | `10.100.99.0/30` | — | Transit: OPNsense `.1` ↔ switch `.2` |

Three changes from that original draft:

- **The `10.10.x.x` addressing is dropped** in favour of the existing `10.100.0.0/16`.
- **The separate LB-VIP VLAN (was VLAN 30) is removed.** Cilium LB-IPAM with L2 announcements
  requires the VIP to be in the same subnet as the interface announcing it, so a dedicated VIP
  VLAN would mean a second NIC on every Kubernetes node for no benefit. The VIP range is carved
  out of VLAN 20 instead. BGP would be the alternative, but the 5130 EI does not speak it and
  the only other candidate speaker is a 1 Gb router. → affects **D10**.
- **The storage/replication VLAN (was VLAN 40) is removed.** With Proxmox CSI and a NAS LXC on
  the same host (D7), there is no storage replication traffic to isolate.

Note that the `nas` LXC lives in VLAN 20, not VLAN 10, even though it is a stage 1 component:
it is a service consumed by clients (SMB) and by the cluster (NFS), and the trust hierarchy in
§4.5 would otherwise block SMB from the client VLAN.

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

**Exactly one trunk in the whole estate.** Make VLAN 10 its PVID (untagged) rather than tagging
everything:

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

Very little, but two items are load-bearing and are the classic way this design fails silently:

- **Static route** `10.100.0.0/16 → 10.100.99.2`.
- **Manual outbound NAT rule for `10.100.0.0/16`.** Automatic outbound NAT covers only
  *directly connected* networks, so with the router in a /30 transit VLAN, every real VLAN gets
  no internet at all until this exists.
- Firewall rules on the transit interface must permit source `10.100.0.0/16`, not just the /30.

Per-VLAN **internet egress policy still lives on OPNsense** and is still stateful — it can
match on source subnet exactly as before. Only east-west policy moved to the switch.

### 4.5 East-west policy — the trust hierarchy

Stateless ACLs are usually painful because you must hand-write return-traffic rules. A trust
ordering removes almost all of that:

```
mgmt (10)  >  servers (20)  >  clients (40)  >  iot (50)
```

ACLs are applied **inbound on each VLAN interface**, i.e. to traffic *sourced by* that VLAN.
Higher tiers may initiate downward freely; lower tiers get explicit permits only. The trick:
if `clients → mgmt` is denied at VLAN 40's ingress, connections from 40 into 10 never exist, so
there is never any return traffic to match — VLAN 10's ACL permits everything anyway. Explicit
`established`/TCP-flag matching is then needed only for the handful of asymmetric permits.

| From | To | Policy |
|---|---|---|
| mgmt (10) | anywhere | permit |
| servers (20) | mgmt (10) | **deny**, except `→ 10.100.10.41:8006` (Proxmox API, needed by Proxmox CSI) |
| servers (20) | clients (40) | deny except established |
| clients (40) | mgmt (10) | **deny**, except the admin workstation's static IP → PVE/iDRAC/`runner` |
| clients (40) | servers (20) | permit 443 (Gateways), 445 (SMB); deny the rest |
| iot (50) | anything internal | deny |
| any | internet | permit — OPNsense filters it |

Worked example for the client VLAN:

```
acl advanced 3040
 rule 5   permit udp destination-port eq bootps                                    # DHCP relay
 rule 10  permit ip  source 10.100.40.99 0 destination 10.100.10.0 0.0.0.255       # admin PC
 rule 20  deny   ip  destination 10.100.10.0 0.0.0.255
 rule 30  permit tcp destination 10.100.20.0 0.0.0.255 destination-port eq 443
 rule 40  permit tcp destination 10.100.20.0 0.0.0.255 destination-port eq 445
 rule 50  deny   ip  destination 10.100.20.0 0.0.0.255
 rule 100 permit ip                                                                # internet
interface Vlan-interface 40
 packet-filter 3040 inbound
```

Four things that are easy to get wrong:

1. **Rule 5 is not optional.** DHCP relay (§4.6) breaks without it, and the symptom — clients
   getting no address — looks nothing like an ACL problem.
2. **Rule 10 is the one you will actually depend on daily.** The hierarchy otherwise forbids
   your PC from reaching the Proxmox UI, iDRAC and the job runner. Give the workstation a
   static reservation.
3. **End every ACL with an explicit `permit ip`** rather than relying on Comware's implicit
   default action for unmatched packets.
4. **Configure the management VLAN's ACL last, from the serial console.** Generate rule numbers
   with gaps (10, 20, 30…) so inserting a rule later does not renumber everything below it.

The 5130's ACL capacity is TCAM-bound; a policy of this size is comfortably within it, but do
not plan on thousands of rules.

### 4.6 DHCP stays on OPNsense; DNS is what `netcore` is for

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
   the most always-on box in the house; the Proxmox host is the thing that will be torn down and
   re-provisioned repeatedly.
2. Kubernetes node addresses come from Cluster API's IPAM provider anyway (§6.1), so DHCP is
   only serving first boot/PXE and household devices — low value, high blast radius.

**DNS: this is `netcore`'s actual job.** The real requirement was never DHCP; it is that stage
3's `external-dns` must *write* records into an internal zone, and OPNsense's Unbound has no
good dynamic-update story. So:

- `netcore` runs **PowerDNS** (or BIND), authoritative for `internal.<domain>` **only**.
- Unbound on OPNsense stays the resolver for every client, with a domain override / stub zone
  for that one zone pointing at `netcore`.
- Clients keep pointing at OPNsense via DHCP. Nothing else changes.

The failure mode is good: if the Proxmox host is down, the internal zone goes with it — but
every name in that zone resolves to cluster Gateways that are also down. General internet DNS
never breaks. → resolves **D4**, and makes `netcore` materially smaller than originally planned.

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
      lb_pool: 10.100.20.200-10.100.20.250 }
  - { id: 40, name: clients, subnet: 10.100.40.0/24, svi: .2, dhcp: true }
policy:
  - { from: clients, to: mgmt,    action: deny, except: [10.100.40.99] }
  - { from: clients, to: servers, ports: [443, 445] }
```

Stage 1 reads the same file for `bridge-vids`; stage 3 generates its
`CiliumLoadBalancerIPPool` from `lb_pool`.

**OPNsense.** Split it: **bootstrap** (interface assignment, LAN IP, enabling the API) is a
one-time manual job — automating it is where a weekend disappears. **Everything mutable** —
filter rules, aliases, outbound NAT, the static route, Unbound overrides, Kea subnets and
reservations — goes through the REST API from Ansible. The `ansibleguy.opnsense` collection
covers most of this; **verify its current Kea coverage**, since Kea replaced ISC dhcpd
relatively recently and module support may lag.

Independently of any of that, turn on OPNsense's **built-in Git backup** (*System →
Configuration → Backups → Git*). It commits `config.xml` on every change, which gives
versioning, a diff trail and a restore path for a few minutes of work — and it is how you will
notice the change you made in the UI at 1am that the playbooks do not know about.

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

**Deferred — IoT/guest separation (VLAN 50).** The living-room switch is unmanaged, so
everything behind it is VLAN 40, including whatever wireless APs currently hang off it. That is
accepted for now. When it matters, in increasing cost:

1. **Run a second cable** from the 5130 to the living room as an access port in VLAN 50.
2. **Replace the living-room switch** with a managed one and make the uplink a trunk.
3. **Move the APs onto the 5130 directly** — this is the real answer for wireless IoT, since a
   VLAN-aware AP maps SSID → VLAN and "IoT" and "Home" become two SSIDs on one radio. It needs
   the AP on a trunk port, which an unmanaged switch downstream cannot cleanly provide.

Decide where wireless lands early: today it presumably hangs off the living-room switch, which
puts every phone and every smart plug in VLAN 40 alongside the workstation.

**Deferred — 2 spare OPNsense ports.** Nothing needs them. If WAN ever exceeds 1 Gb, LACP two
of them to the switch; note that this raises aggregate throughput, not per-flow.

### Stage 0 exit criteria

- [ ] `verify.yml` reports zero drift on both devices from a clean checkout
- [ ] A client in VLAN 40 gets an address via relay and reaches the internet
- [ ] The PC (VLAN 40) sustains ≥ 5 Gbit/s to a VM in VLAN 20 — proving the traffic is switched,
      not routed through OPNsense
- [ ] The trust hierarchy holds: an unprivileged VLAN 40 host cannot reach `10.100.10.0/24`,
      and the admin workstation can
- [ ] Serial console access to the 5130 is tested and documented
- [ ] A deliberately broken config is rolled back by the SAFETY-NET job without intervention

---

## 5. Stage 1 — Bootstrap

**Tool:** Ansible, run from your workstation against the Proxmox host over SSH.
**Property:** idempotent, re-runnable, and functional with the entire Kubernetes estate down.

### 5.1 Proxmox host configuration
- Enterprise repo off / no-subscription repo on, unattended-upgrade policy
- **Create `hddpool`** — 4× SAS HDD as ZFS RAID-Z1 (§2.3). The 4th drive doesn't need to match
  the others' model — same interface (SAS), comparable capacity and RPM is enough. Confirm the
  drives present as raw devices first: the PERC must expose them as non-RAID/HBA, as it
  evidently already does for the SATA SSDs. Datasets: `hddpool/nas`, `hddpool/pbs`, plus a
  Proxmox storage for `proxmox-hdd` volumes.
- **ZFS tuning — the highest-value items on this hardware:**
  - **`volblocksize` ≥ 16K** on both ZFS storages. On 3-disk RAID-Z1 the old 8K default
    costs ~166% of nominal space (§2.2). Verify the current setting; note it only applies to
    newly created volumes, so existing disks must be migrated to change it.
  - `recordsize` on `hddpool/nas` tuned for bulk files (1M) rather than left at the 128K default
  - **ARC cap.** Default ARC is 50% of RAM = 256 GB. With 512 GB that is affordable and a
    large ARC genuinely helps, but set it explicitly rather than leaving it to chance —
    something in the 64–128 GB range leaves plenty for VMs while giving a big read cache.
  - Dataset layout, `compression=lz4` (or `zstd`), `atime=off`, autotrim for SSDs
  - Scheduled scrub + SMART monitoring with alerting
- **NUMA:** expose NUMA topology for larger VMs, or size them to fit within one socket (§2.2)
- Linux bridges and VLAN tags — VLAN-aware `vmbr0`, `bridge-vids` from `network.yml` (§4.3, §4.7)
- Host firewall rules, NTP, email/alert relay
- **iDRAC:** configure out-of-band management, and consider the Redfish exporter so hardware
  health (PSU, fans, drive SMART, temperatures) lands in the stage 3 monitoring stack
- API token + role for stage 2's Proxmox provider (least privilege, not `root@pam`)
- **Proxmox Backup Server** with an **S3 datastore pointed at Backblaze B2** (supported since
  PBS 4.2), client-side encryption enabled, plus a sized local cache — see §7.7
- Optional: PCIe/GPU passthrough config if you want hardware transcoding or GPU workloads

### 5.2 `netcore` LXC — authoritative internal DNS
Hosts the **internal** DNS zone that stage 3's `external-dns` writes into, giving split-horizon
DNS (internal names resolve to internal Gateway IPs; the same names on the public internet
resolve to Cloudflare). PowerDNS, authoritative for `internal.<domain>` and nothing else.

**It does not serve DHCP, and it is not the network's resolver** — both stay on OPNsense, which
forwards just this one zone here via a domain override (§4.6). That keeps household DHCP and
general name resolution off the machine this project exists to rebuild repeatedly, and shrinks
`netcore` to the one job OPNsense genuinely cannot do: accept dynamic updates.
→ **Decision D4.**

### 5.3 `runner` LXC — web UI and job execution
The "run stage 2 from a GUI" requirement, and the break-glass console. Must be outside
Kubernetes so it still works when Kubernetes doesn't. Runs Ansible and OpenTofu jobs against
Proxmox and against the management cluster.
→ **Decision D5.**

### 5.4 `nas` LXC — SMB + NFS file services
Bind-mounts `hddpool/nas` and exports it two ways (§2.3):

- **SMB** for household/desktop access — available whether or not Kubernetes is running
- **NFS** as the backing export for the cluster's `nfs-nas` RWX volumes (static PVs, no CSI
  driver)

Both views are the *same dataset*, so Nextcloud in the cluster and a laptop over SMB see the
same files. Users, shares and exports are all defined in Ansible. Snapshots come from ZFS on
the host; backup is restic/rclone to B2 (§7.7).

### 5.5 Management cluster VM + Flux bootstrap
Single-node Kubernetes, created by Ansible, with `flux bootstrap` pointed at this repo's
`stage2/` path. From this moment on, stage 2 is Git-driven.
→ **Decisions D2, D3.**

### 5.6 Secrets root
Stage 1 generates the **age** keypair and installs the private key as a Kubernetes Secret in
both Flux instances (`sops-age` in `flux-system`), so Flux can decrypt at reconcile time. It
also configures the private secrets repo as a second `GitRepository` source (§8.1).

The private key is the one thing that **cannot** live in any repo — password manager plus an
offline copy. Losing it means every encrypted value in Git is unrecoverable; leaking it means
every encrypted value ever committed is exposed. Back it up before you rely on it.
→ **Decision D11 (SOPS + age), §8.1.**

### 5.7 Network — see §4

The VLAN, addressing and firewall design moved to **stage 0 (§4)**, because stage 1 Ansible
depends on the network already working. What remains stage 1's responsibility is only the
*host* side of it: the VLAN-aware `vmbr0` bridge and the per-VM VLAN tags (§4.3), with
`bridge-vids` rendered from the same `network.yml` that drives the switch (§4.7).

Note that Cluster API's in-cluster IPAM provider assigns node IPs statically, so DHCP is only
needed for PXE/first boot and for non-Kubernetes devices — which is part of why DHCP stays on
OPNsense rather than moving into `netcore` (§4.6).

### Stage 1 exit criteria
- [ ] Ansible run from a clean Proxmox install reproduces the whole stage without manual steps
- [ ] The internal DNS zone resolves, and `external-dns` can write to it
- [ ] `vmbr0` is VLAN-aware and a VM tagged `20` gets an address via relay
- [ ] Web UI reachable and able to run a trivial job
- [ ] Management cluster is up and Flux reports the `stage2/` path as reconciled
- [ ] Secrets root is provisioned and backed up off-box

---

## 6. Stage 2 — Cluster lifecycle

Everything here is YAML in `stage2/`, reconciled by the management cluster's Flux.

### 6.1 Components
- **cert-manager** (a Cluster API prerequisite, for webhook certs)
- **Cluster API core** + bootstrap provider + control-plane provider + **Proxmox
  infrastructure provider** + **IPAM provider** → D1, D2
- **ClusterClass + Cluster topology** — strongly recommended. With a ClusterClass, a
  Kubernetes upgrade is `spec.topology.version: v1.34.x` in one file and the controllers
  roll control plane and workers in the right order. This is what makes "upgrades as GitOps"
  real rather than a pile of scripts.
- **cluster-autoscaler** (`--cloud-provider=clusterapi`) → §6.4
- **Node image pipeline** — Packer builds a versioned Proxmox VM template, or, with Talos,
  simply importing the published `nocloud` image per release. → influenced by D1
- **Maintenance automation** — cron- and manually-triggerable jobs → §6.5

### 6.2 Certificate rotation
This is largely solved by the controllers rather than by scripts:

- **kubeadm-based:** set `KubeadmControlPlane.spec.rolloutBefore.certificatesExpiryDays: 90`.
  Cluster API proactively rolls control-plane machines before their certs expire, and the
  replacement machines get fresh PKI. Rotation becomes a side effect of normal machine
  lifecycle.
- **Talos-based:** Talos manages its own PKI and rotates most material itself; the Talos
  control-plane provider handles the rest on machine rollout.

Either way the correct answer is "roll the machine", not "run a rotation script on a live
node". Add a monitoring alert on certificate expiry as a backstop.

### 6.3 Side infrastructure
Depending on D7, the storage layer may be provisioned here (host-side ZFS datasets, a
dedicated storage VM, or Proxmox storage definitions) rather than in stage 3. The
CSI *driver* installation belongs to whichever stage owns the cluster addons — see §7.1.

### 6.4 Autoscaling — and the single-host trap

`cluster-autoscaler` watches for unschedulable pods and scales the `MachineDeployment`
replica count; Cluster API then creates VMs. Scale-from-zero needs capacity annotations
because the Proxmox provider doesn't advertise machine capacity:

```yaml
metadata:
  annotations:
    cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size: "0"
    cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: "4"
    capacity.cluster-autoscaler.kubernetes.io/memory: "16G"
    capacity.cluster-autoscaler.kubernetes.io/cpu: "4"
    capacity.cluster-autoscaler.kubernetes.io/ephemeral-disk: "50Gi"
```

**Three traps specific to this setup:**

1. **The autoscaler has no idea the Proxmox host is finite — and on this box the limit is
   disk, not RAM.** With 512 GB of RAM you will never exhaust memory, but `max-size × root
   disk size` comes straight out of a 1.44 TB working budget that also holds every PVC. Set
   `max-size` from the §2.6 budget and alert on pool usage crossing 75%. Also disable memory
   ballooning on Kubernetes node VMs — ballooning plus kubelet's view of available memory is
   a bad combination, and with this much RAM there is no reason to overcommit anyway.
2. **Replicated node-local storage blocks scale-down.** Longhorn and Ceph place replicas on
   nodes; the autoscaler then refuses to drain those nodes (or worse, drains them and
   degrades your volumes). This is the single most common way these two features break each
   other.
3. **Scale-down needs discipline elsewhere:** PodDisruptionBudgets on anything that matters,
   and `cluster-autoscaler.kubernetes.io/safe-to-evict` annotations on pods with local
   storage that are actually safe to move.

**Mitigation — split node pools.** This resolves trap 2 and 3 cleanly:

| Pool | Autoscaled | Runs |
|---|---|---|
| `control-plane` ×3 | no | control plane only |
| `system` (fixed, 2–3 nodes) | no | storage replicas, Postgres, monitoring, ingress — anything stateful or with a PDB |
| `burst` (0 → N) | **yes** | stateless workloads only, via taint + toleration or nodeSelector |

The burst pool holds no persistent state, so it can be destroyed freely.

### 6.5 Maintenance tasks (cron + manual trigger)
Candidates: Kubernetes/Talos version upgrades, node image rebuilds, etcd backup verification,
certificate expiry checks, storage scrub/rebalance, Proxmox host updates, backup restore
drills. Each should be a job that is idempotent, logs to one place, and can be triggered
both on a schedule and by a human clicking a button.
→ **Decision D6.**

### Stage 2 exit criteria
- [ ] Deleting the workload cluster and letting Flux reconcile rebuilds it unattended
- [ ] A Kubernetes version bump in one file rolls the whole cluster with no manual steps
- [ ] Autoscaler scales the burst pool up under load and back to zero afterwards
- [ ] Cert expiry is monitored and rotation is proven by a forced rollout

---

## 7. Stage 3 — Platform & applications

Flux on the workload cluster, reconciling `stage3/`.

### 7.1 The stage 2 / stage 3 boundary
Explicit rule: **stage 2 owns everything required for the cluster to reach the point where
Flux can run.** That is CNI, cloud-controller-manager, and Flux itself — installed via
Cluster API's Helm add-on provider or a ClusterResourceSet. **Stage 3 owns everything
above that line**, including CSI drivers and storage classes. Without this rule, CNI
ownership in particular ends up ambiguous.

### 7.2 Proposed layout

```
stage3/
  clusters/homelab/          # Flux entrypoint; Kustomizations with dependsOn
  infrastructure/
    controllers/             # CNI extras, cert-manager, external-dns, gateway, CSI, ESO
    configs/                 # ClusterIssuers, Gateways, StorageClasses, IPPools
  observability/
  databases/
  apps/
```

Reconciliation order via `dependsOn`: `controllers` → `configs` → `storage` →
`observability` → `databases` → `apps`.

### 7.3 Ingress — two entry points

⚠️ **ingress-nginx reached end of life in March 2026** — no releases, no bugfixes, and no
security patches. Its intended successor, InGate, was cancelled before maturity. The
Kubernetes project's recommended path forward is the **Gateway API**. This plan therefore
uses Gateway API rather than Ingress; the "two ingress classes" requirement becomes two
`Gateway` resources (optionally two `GatewayClass`es).
→ **Decision D9.**

| | External Gateway | Internal Gateway |
|---|---|---|
| Reachability | Public, proxied through Cloudflare | LAN / VPN only |
| LB IP | e.g. `10.100.20.200`, port-forwarded | e.g. `10.100.20.201`, never forwarded |
| Certificates | Cloudflare **Origin CA** certs | Let's Encrypt, DNS-01 via Cloudflare |
| Issuer | `cloudflare/origin-ca-issuer` (cert-manager external issuer — automates Origin cert issuance/renewal, so no manual 15-year cert handling) | cert-manager `ClusterIssuer` with the Cloudflare DNS-01 solver |
| DNS | `external-dns` → Cloudflare, proxied records | `external-dns` → internal DNS zone on `netcore` |

Split-horizon DNS means the same hostname can resolve internally to the internal Gateway and
externally via Cloudflare. Two `external-dns` instances, filtered by annotation or by
Gateway, keep the two record sets separate.

LB IP assignment → **Decision D10.**

### 7.4 Storage
The most consequential decision in the whole plan, and the one where the single-host
constraint bites hardest. → **Decision D7.** Requirements to weigh:

- **RWO block** for Postgres and most apps — needs to be fast and to survive node churn
- **RWX shared** for media libraries, shared config, some apps
- **S3-compatible object storage** for CNPG backups and Velero → **D16**
- Snapshots + a path to off-box backup → **D14**
- Must not fight the autoscaler (§6.4)

### 7.5 Monitoring
Metrics, logs, dashboards and alert routing, with dashboards provisioned as code rather than
clicked into Grafana. → **Decision D12.**

### 7.6 PostgreSQL
**CloudNativePG** is the recommendation (see D13) — a 3-instance cluster with synchronous
replication, anti-affinity across the `system` pool nodes, rolling minor-version upgrades,
and continuous backup to object storage with point-in-time recovery.

Reality check per §2: on one host, 3 instances give you zero-downtime upgrades and fast
failover from a VM or pod failure, not survival of a host failure. **The backup destination
must be off this machine** or the entire HA setup is theatre — see D14/D16.

### 7.7 Backup & disaster recovery — Backblaze B2

**Decided: Backblaze B2 as the off-box backup target** (2026-08-16). This section spans all
three stages — PBS is configured in stage 1, CNPG and Velero in stage 3 — but it's collected
here because it only makes sense as one design.

Per §2.2, this is the part that provides actual resilience. RAID-Z1 survives a disk; B2
survives the chassis, the room, and a ransomware event.

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

The SAS tier makes a genuine 3-2-1 arrangement possible, which is better than the B2-only
design this section originally carried:

1. **Local datastore on `hddpool`** (~1 TB budgeted, §2.6) — VMs restore at disk speed rather
   than over the internet. This is the copy you'll actually use, for the overwhelmingly common
   case of "I broke a VM".
2. **Sync job → S3 datastore on B2** — the copy that survives the chassis, the room, and
   ransomware. PBS 4.2 added exactly this: *S3 Datastore → Sync Jobs → Add*, source location
   `Local`. Push sync jobs can encrypt on the fly before transmission, and the new
   `worker-threads` property parallelises groups to raise throughput on a high-latency link.
3. **Chunk cache on the boot NVMe** (~100 GB) — limits S3 API calls. Regenerable, which is why
   it's appropriate for drives you don't fully trust (§2.3).

PBS gained native S3 datastore support in 4.0 (August 2025) and it **left tech preview in 4.2
(April 2026)**, so this is a supported configuration rather than a hack. Deduplication,
compression, client-side encryption, pruning, verification and garbage collection all still
apply. Configure the endpoint under *Configuration → Remotes → S3 Endpoints*.

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
using the very credentials stored there — which is how ransomware turns a recoverable
incident into a total loss.

⚠️ **Verify before relying on it for `homelab-pbs`:** PBS garbage collection needs to delete
unreferenced chunks, and compliance-mode Object Lock will prevent exactly that, so GC can
fail and the datastore grows without bound. Test PBS + Object Lock on a throwaway bucket
first; governance mode (which permits privileged deletion) may be the workable compromise.
Don't assume this works — confirm it.

#### Known B2 + CloudNativePG friction

This is documented upstream and costs real time if you hit it blind:

- **`HeadBucket` fails against B2** even with valid credentials and permissions
  (cloudnative-pg issue #7105).
- **The `region` key does not populate `AWS_DEFAULT_REGION`** (issue #9724) — set the
  environment variable explicitly.
- Set **`BARMAN_S3_USE_PATH_STYLE=true`** and `AWS_DEFAULT_REGION` in
  `instanceSidecarConfiguration`.
- Endpoint URL takes the form `https://s3.<region>.backblazeb2.com`.
- Build on the **Barman Cloud plugin (CNPG-I)** rather than the older in-tree
  `barmanObjectStore` field, which is on its way out. Check its current status when you get
  to phase 10.

#### Cost

At roughly **$6–7/TB/month**, a realistic 400–700 GB of deduplicated backups costs about
**$3–5/month**. Egress is free up to 3× average monthly storage, so a full restore is
effectively free — which removes the usual excuse for never testing one.

#### Restore drills

An untested backup is not a backup. Schedule as a stage 2 maintenance job (§6.5):

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
├── stage2-cluster/            # reconciled by mgmt-cluster Flux
│   ├── flux/
│   ├── providers/             # CAPI + Proxmox + IPAM + autoscaler
│   ├── clusters/homelab/      # ClusterClass + Cluster topology
│   └── maintenance/           # scheduled + manual jobs
└── stage3-platform/           # reconciled by workload-cluster Flux
    ├── clusters/homelab/
    ├── infrastructure/
    ├── observability/
    ├── databases/
    └── apps/
```

→ **Decision D15** on monorepo vs split repos.

### 8.1 Repository visibility — public config, private secrets

**Decided (2026-08-16): this repo goes public; a second private repo holds encrypted secrets.**

#### Why not SOPS-encrypted secrets in the public repo

The cryptography is sound — age uses X25519 + ChaCha20-Poly1305 and the ciphertext is opaque.
The problem is the failure modes, all of which get materially worse when the ciphertext is
public:

1. **Git history is permanent, and public is irreversible.** If the age private key ever leaks
   — laptop compromise, a bad backup, an accidental commit — then *every secret ever committed,
   across all history*, becomes decryptable by anyone who cloned or archived the repo. It will
   have been archived (forks, GHArchive, Software Heritage). Rotating afterwards protects the
   future, not the past. With a private repo, a key leak still requires repo access: **both**
   have to fail.
2. **The realistic failure is a plaintext commit, not broken crypto.** `.sops.yaml` uses
   path-based creation rules; a new directory matching no rule gets committed unencrypted,
   silently. Public repos are scraped by bots within minutes.
3. **SOPS encrypts values, not keys.** Field names, file structure, namespace names and any
   unencrypted fields remain readable — infrastructure metadata, not noise.

Encrypted **and** private is genuine defence in depth, and costs one extra repository.

#### The arrangement

| Repo | Visibility | Contains |
|---|---|---|
| `homelab` (this one) | **public** | All stages' manifests, Ansible, docs — everything except secret material |
| `homelab-secrets` | **private** | SOPS+age encrypted `Secret` manifests only |

Flux consumes both via separate `GitRepository` sources. Coupling is minimal because
Kubernetes Secrets are referenced **by name**, not by path: the private repo delivers `Secret`
objects into namespaces, and the public repo's workloads reference them via `secretRef`.
Neither repo needs to know the other's layout.

#### Guardrails (implement in phase 0, before the repo goes public)

- **Decide before publishing, not after.** A repo that ever contained secrets and is later
  flipped to public exposes its entire history. This repo is currently a single empty commit,
  so it is clean — keep it that way.
- `.sops.yaml` creation rules with **broad** path regexes, so new directories are covered by
  default rather than by remembering.
- Pre-commit hook (`gitleaks` or equivalent) blocking unencrypted secret material.
- CI check asserting every file matching `*secret*.y*ml` is actually SOPS-encrypted.
- GitHub secret scanning **and** push protection enabled on both repos.
- **Treat any accidental plaintext commit as full compromise** — rotate the credential, don't
  just `git rm` it. History rewriting does not reach forks or archives.

#### Never in Git, in any repo or form

- The **age private key** (password manager + offline copy — see §5.6)
- The **B2 master application key** (use per-bucket scoped keys, §7.7)
- Anything that cannot be rotated

#### One consequence for this document

A public repo publishes your hardware inventory, IP plan and service topology — useful
reconnaissance for anyone targeting you. It's behind a firewall and this is a homelab, so the
practical risk is low, but it should be a deliberate choice rather than an accident. If you'd
rather not publish it, keep §2.1 and §4.2 generic in the public copy and hold the specifics in
the private repo.

---

## 9. Decision register

Mark your choice on each. **Tier 1** decisions block implementation; **Tier 2** can be
deferred until the relevant stage. A recommendation is given for each — where I have a
strong opinion I say so, and where it's close I say that too.

---

### Tier 1 — needed before any code is written

#### D1. Node operating system & Kubernetes bootstrap

| Option | Pros | Cons |
|---|---|---|
| **A. Talos Linux** ⭐ | Immutable, no SSH, minimal attack surface; API-driven upgrades; no image-building pipeline (import the published `nocloud` image per release); tiny RAM footprint; designed for exactly this | Very different mental model — no shell to debug in; some Helm charts assume a normal distro; extra CAPI providers needed (Talos bootstrap + control plane) |
| **B. Ubuntu/Debian + kubeadm** | Familiar; debuggable with normal tooling; the best-trodden CAPI path; every guide applies | You now own a Packer image pipeline and OS patching; larger footprint; more configuration drift surface |
| **C. RKE2 / K3s + Rancher** | Batteries included; Rancher gives a strong web UI for free; lower conceptual overhead | Rancher's Proxmox integration is weaker than CAPI's; autoscaling story is less clean; more opinionated, harder to escape later |

**Recommendation: A (Talos).** Immutability plus API-driven upgrades removes a whole class
of day-2 work, and skipping the Packer pipeline is a large saving. Choose B if being able to
SSH into a broken node at 1am matters more to you than the operational savings — that's a
legitimate preference, not a wrong answer.

**Choose:** ☑ **A — Talos Linux** *(decided 2026-08-16)*  ☐ B  ☐ C

---

#### D2. Cluster lifecycle engine

| Option | Pros | Cons |
|---|---|---|
| **A. Cluster API + Proxmox provider (CAPMOX) + IPAM** ⭐ | Fully declarative — cluster, upgrades and repair are all YAML; the only option with first-class `cluster-autoscaler` support; ClusterClass makes upgrades a one-line change; combines cleanly with Talos | CAPMOX is a modest project (~470 stars) and still on `v1alpha2` with `v1alpha3` in flight — expect breaking API changes; more moving parts; needs a management cluster |
| **B. OpenTofu/Terraform + `bpg/proxmox`** | Simple, very widely used, easy to reason about; no management cluster needed | Not a reconciler — drift and node repair are your problem; autoscaling requires writing and maintaining a custom controller; upgrades become imperative runbooks |
| **C. Sidero Omni (self-hosted)** | Excellent UX and built-in web UI; free for homelab use under BUSL; Talos-native; very polished | BUSL licence (non-production only); doesn't provision Proxmox VMs — you still need something to create them; no native cluster autoscaling; note Sidero Metal, the CAPI-based predecessor, is discontinued |
| **D. Rancher** | Best-in-class UI; node pools with scaling | Weakest Proxmox integration of the four; heavy; pulls you into the Rancher ecosystem |

**Recommendation: A.** It is the only option that genuinely delivers the autoscaling
requirement declaratively. The CAPMOX maturity risk is real and worth pinning provider
versions and reading release notes before upgrades — but the alternative (B) means writing
your own autoscaler, which is strictly more risk.

**Choose:** ☑ **A — Cluster API + CAPMOX** *(decided 2026-08-16)*  ☐ B  ☐ C  ☐ D

---

#### D3. Management-plane placement

| Option | Pros | Cons |
|---|---|---|
| **A. Dedicated single-node cluster VM, created by stage 1** ⭐ | Clean separation; survives workload-cluster loss; makes "stage 2 is GitOps" literally true; simple to reason about | Costs ~4–6 GB RAM permanently; one more thing to maintain and upgrade |
| **B. `kind` cluster on the runner LXC, pivot to self-hosted** | No permanent RAM cost | The workload cluster manages itself — if it's badly broken it cannot repair itself, which defeats the point; pivot is fiddly |
| **C. CAPI controllers in Docker on the runner LXC** | Lightest | Non-standard, poorly supported, you'll be on your own |

**Recommendation: A.** It's the difference between "the cluster rebuilds itself" and "I rebuild
the cluster". With 512 GB of RAM the memory cost is a rounding error, so the only real cost is
the ~40 GB of disk in the §2.6 budget.

**Choose:** ☑ **A — Dedicated single-node management cluster VM** *(decided 2026-08-16)*  ☐ B  ☐ C

---

#### D7. Storage — **the big one**

Requirements: RWO block (fast, for Postgres), RWX shared, S3 for backups, snapshots, and it
must not block the autoscaler.

| Option | Pros | Cons |
|---|---|---|
| **A. Rook-Ceph** | Block + file + S3 in one system; the "real" answer at scale; genuinely great multi-node | **Poorly suited to one host.** Guidance is not to run it under 32 GB RAM per node; replication across OSDs on one box costs 3× IO and RAM for no protection against disk or host loss; steep operational learning curve; replicas pin nodes and fight scale-down |
| **B. Longhorn** | Installs in minutes; built-in UI; snapshots and S3 backup built in; RWX via NFS; good fit for small clusters — the usual homelab default | Replica placement pins nodes → fights the autoscaler (mitigable with the `system` pool); NFS-based RWX is a performance and failure-mode compromise; another replication layer on top of ZFS |
| **C. Proxmox CSI plugin** | Volumes are plain Proxmox (ZFS/LVM-thin) disks — host ZFS already gives redundancy and snapshots, with no duplicated replication; low overhead; volumes can be inspected by mounting them elsewhere; detach/attach works well with node churn | RWO only — no RWX, no S3; requires the single Proxmox node to be "clustered with itself"; a volume is tied to the host (fine — there's only one); smaller project |
| **D. democratic-csi → ZFS over NFS/iSCSI** | Leans on ZFS directly; mature; gives both RWO (iSCSI) and RWX (NFS) | Needs a NAS VM or host-side export configured in stage 1/2; another service in the data path |
| **E. Hybrid: Proxmox CSI (RWO) + Longhorn *or* NFS (RWX) + Garage (S3)** ⭐ | Each workload gets the right primitive; Postgres gets low-overhead block storage; RWX and S3 exist without paying Ceph's tax | Three systems instead of one; more to learn and monitor |
| **F. SeaweedFS** | The closest thing to "one system for everything" short of Ceph: S3 API + POSIX filer + a CSI driver that does RWX; far lighter than Ceph | PVCs are FUSE mounts of the filer, so database workloads are a poor fit — you'd still want block storage for Postgres; smaller community than Longhorn/Ceph; another distributed system to operate |

**Decided: E, simplified further** *(2026-08-17)* — the two-tier storage layout in §2.3 makes
the RWO/RWX split clear-cut, and two more pieces of the original hybrid turned out to be
solving problems this deployment doesn't have: a dedicated in-cluster S3 system (Garage) for a
Loki instance that can just use a filesystem-backed PVC, and a CSI driver for RWX volumes when
there are only one or two of them. Both are cut. What remains:

| Need | Component | Backed by | Overhead |
|---|---|---|---|
| Hot RWO block (Postgres, metrics TSDB) | **Proxmox CSI** → `proxmox-ssd` | `ssdpool` | 1× |
| Bulk RWO block (media, Loki chunks) | **Proxmox CSI** → `proxmox-hdd` | `hddpool` | 1× |
| RWX shared (Nextcloud, multi-pod) | **Static PV/PVC**, no CSI driver | `hddpool/nas`, exported by the `nas` LXC | 1× |
| S3 (backups only) | **Backblaze B2** — off-box, §7.7 | — | — |

The RWX question that was holding this decision open is answered by §2.3: the NAS dataset and
the RWX volumes point at **the same dataset**, so SMB users and cluster workloads see the same
files and there's no second system to run. There's no in-cluster S3 at all right now — if a
future app genuinely needs one (something that only speaks S3), Garage is a cheap add-back at
that point; this isn't a door closed, just one not walked through before it's needed.

Why, given §2.6:

- **RAID-Z1 already survives a disk failure on both pools.** Longhorn or Ceph replication on
  top would cost 2–3× of the SSD tier to protect against something ZFS already handles — the
  difference between ~1.07 TB and ~357 GB of usable hot application data.
- **Proxmox CSI doesn't fight the autoscaler.** Volumes detach and reattach to whichever VM
  needs them, within one host, with no data movement. This is the trap-2 problem in §6.4
  simply not applying. NFS is node-agnostic for the same reason.
- **RAM abundance doesn't rescue Ceph.** Ceph's 32 GB/node appetite is affordable here, but
  its OSDs would sit on zvols on top of ZFS — copy-on-write on copy-on-write, an anti-pattern
  with bad write amplification — and it would still triple your data on the scarcest resource.
  **A remains a clear no.**
- **Tiering is free.** Proxmox CSI exposes each Proxmox storage as its own StorageClass, so
  `proxmox-ssd` / `proxmox-hdd` requires no extra component — just two ZFS pools on the host.

Revisit Ceph seriously if you add a second and third physical node — the plan should keep
that door open, and Proxmox CSI does not close it.

**Caveat to handle in stage 1:** `proxmox-csi-plugin` requires the Proxmox node to be part of
a cluster, so the single node must be "clustered with itself" (`pvecm create`). It's a one-time,
low-risk step, but it must happen before the CSI driver will provision anything.

**No option here is object-storage-only, deliberately.** Every candidate provides block or
POSIX volumes as its primary function, with S3 layered on top — see the note under D16 for
why an S3-only solution (MinIO, Garage, or any other) cannot serve PVCs. F and JuiceFS come
closest to unifying the three, but both still want real block storage under the databases.

**Choose:** ☐ A  ☐ B  ☐ C  ☐ D  ☑ **E (simplified) — Proxmox CSI (tiered) + static NFS RWX, no in-cluster S3** *(decided 2026-08-17)*  ☐ F

---

#### D11. Secrets management

| Option | Pros | Cons |
|---|---|---|
| **A. SOPS + age** ⭐ | Native Flux decryption; secrets encrypted in Git; trivial to operate; no extra infrastructure | No rotation or audit; key distribution is manual; encrypted blobs in Git forever (a leaked old key exposes all history) |
| **B. Sealed Secrets** | Simple; cluster holds the private key | Sealed to one cluster — awkward when you rebuild the cluster; no rotation story |
| **C. External Secrets Operator + backend** (Vault / Infisical / 1Password / Bitwarden) | Real rotation, audit, and a UI; **secrets never in Git in any form**, so `ExternalSecret` manifests are safe to publish; one source of truth across both clusters | Extra service to run and back up; a bootstrap dependency (ESO needs credentials to start) |

**Decided: A (SOPS + age)** *(2026-08-16)*, **with encrypted secrets in a separate private
repository** — see §8.1. **C remains the upgrade path** and becomes more attractive once the
public repo exists, since ExternalSecret manifests carry only references and can live in the
open repo as documentation.

**Choose:** ☑ **A — SOPS + age, secrets in a private repo** *(decided 2026-08-16)*  ☐ B  ☐ C

---

### Tier 2 — can be decided at the relevant stage

#### D4. DHCP + DNS — *promoted to Tier 1 in effect; decided 2026-08-17*
- **A. dnsmasq in an LXC** — one small daemon does DHCP + DNS + TFTP; trivial to configure in Ansible; ideal if the requirement stays simple.
- **B. Kea DHCP + PowerDNS/BIND** — proper API-driven DNS that `external-dns` can write to via RFC2136 or the PowerDNS provider; more moving parts. **Pick this if you want internal `external-dns` automation** (§7.3).
- **C. OPNsense/pfSense VM** — full firewall/router with a good UI; replaces your router; much larger scope and a bigger blast radius.
- **D. Existing router** — zero work, but not code, and usually can't do dynamic DNS updates.

**Decided: split D + B** *(2026-08-17)* — the question turned out to contain a false premise.
The plan wanted DHCP off the router because separation was assumed to require router-based
routing; §4.1 shows the L3 switch does that instead, so the constraint evaporated.

| Function | Where | Why |
|---|---|---|
| DHCP (all VLANs, via relay) | **OPNsense** (option D) | Household infrastructure must not live on the host this project rebuilds repeatedly; CAPI IPAM assigns node IPs anyway, so DHCP only serves first boot and family devices |
| Recursive resolver for clients | **OPNsense** Unbound (option D) | Already works; no reason to move it |
| Authoritative `internal.<domain>` | **PowerDNS in `netcore`** (the DNS half of B) | The one thing OPNsense genuinely can't do: accept dynamic updates from `external-dns` |

Unbound gets a domain override for that single zone → `netcore`. Full rationale in §4.6.

**Choose:** ☐ A  ☑ **B — PowerDNS in `netcore`, internal zone only**  ☐ C  ☑ **D — OPNsense keeps DHCP + resolver** *(decided 2026-08-17)*

#### D5. Stage 1 web UI / job runner
- **A. Semaphore UI** ⭐ — single Go binary, no runtime deps, runs Ansible *and* OpenTofu/Terraform, low RAM, actively developed, MIT core. Best fit for an LXC.
- **B. AWX** — powerful and granular, but needs Kubernetes, wants 8 GB+ RAM, is Ansible-only, and has been slow on releases. Wrong shape for a break-glass layer that must work when Kubernetes is down.
- **C. Rundeck** — the most granular ACLs; heavier; enterprise-oriented.
- **D. Gitea Actions / Woodpecker CI** — you get Git hosting and CI in one; more generic and more assembly required.

**Recommendation: A.** It matches the constraint exactly: light, outside Kubernetes, multi-tool.
(**D** is worth considering if you also want to self-host the Git repo — see D15.)

**Choose:** ☐ A  ☐ B  ☐ C  ☐ D

#### D6. Stage 2 day-2 operations UI
- **A. Reuse Semaphore from stage 1** ⭐ — one UI for everything; already exists; good for imperative jobs.
- **B. Argo Workflows (+ Events)** — proper DAG pipelines, cron triggers and manual "run now" in one tool; the best fit for complex multi-step maintenance; adds a workflow engine to the mgmt cluster.
- **C. Flux UI (Weave GitOps OSS / Capacitor / Headlamp + Flux plugin)** — best for *seeing* and forcing reconciliation, not for running arbitrary jobs.
- **D. Plain `CronJob` + `kubectl create job --from=cronjob/...`** — simplest; manual triggering isn't really "through a web UI".

**Recommendation: A + C together.** Semaphore for running things, a Flux UI for observing and
forcing reconciliation. Add **B** later if maintenance grows genuinely multi-step.

**Choose:** ☐ A  ☐ B  ☐ C  ☐ D  (combination fine)

#### D8. CNI
- **A. Cilium** ⭐ — eBPF, high performance; can replace kube-proxy; **includes LB-IPAM and Gateway API support**, potentially collapsing D9 and D10 into one component; Hubble gives excellent network observability.
- **B. Calico** — mature, simple, good policy support; fewer built-in extras.
- **C. Flannel** — simplest; no network policy; you'll outgrow it.

**Recommendation: A.** Folding load-balancer IPAM and Gateway API into the CNI removes two
separate components. Cost is a steeper learning curve.

**Choose:** ☐ A  ☐ B  ☐ C

#### D9. Gateway / ingress implementation (ingress-nginx is EOL — see §7.3)
- **A. Cilium Gateway API** ⭐ (if D8=Cilium) — no extra component; eBPF data path.
- **B. Envoy Gateway** — the reference Gateway API implementation; excellent conformance; actively developed.
- **C. Traefik** — good Gateway API support, familiar, nice dashboard, huge middleware ecosystem, easy Cloudflare integration.
- **D. HAProxy / other Ingress controller** — if you genuinely want to stay on the Ingress API. Works, but you'd be adopting a legacy API for a new build.

**Recommendation: A if D8=Cilium, otherwise C.** Traefik is the gentlest landing if Gateway
API is new to you; Envoy Gateway is the most "correct" choice.

**Choose:** ☐ A  ☐ B  ☐ C  ☐ D

#### D10. Load-balancer IP assignment
- **A. Cilium LB IPAM** ⭐ (if D8=Cilium) — built in, nothing extra to run.
- **B. MetalLB (L2 mode)** — the well-known standard; works with any CNI; one more controller.
- **C. kube-vip** — good for control-plane VIPs; also does service LBs.

**Recommendation: A if Cilium, else B.** Note you likely also need a control-plane VIP —
kube-vip or Talos's built-in VIP covers that regardless.

**Constrained by stage 0** *(2026-08-17)*: whichever of A or B you pick uses **L2/ARP
announcements**, which require the VIP to sit in the same subnet as the announcing node
interface. The VIP range is therefore **carved out of the node VLAN** — `10.100.20.200–.250`
in VLAN 20 (§4.2) — and the separate VIP VLAN from the old draft is deleted. A dedicated VIP
VLAN would mean a second NIC on every Kubernetes node for no benefit.

BGP (Cilium's BGP Control Plane advertising a routed VIP prefix) is the alternative that would
allow a separate VLAN, but the HPE 5130 EI does not speak BGP and the only other candidate peer
is a 1 Gb router. Not worth it at this scale; revisit only if the switch is ever replaced.

**Choose:** ☐ A  ☐ B  ☐ C — *VIP range fixed to `10.100.20.200–.250` regardless (§4.2)*

#### D12. Monitoring stack
- **A. kube-prometheus-stack** — Prometheus + Grafana + Alertmanager + a large set of dashboards and alert rules out of the box. The default for a reason. RAM cost is irrelevant on this hardware; **disk cost is not**.
- **B. VictoriaMetrics stack (+ Grafana + Alertmanager)** ⭐ — substantially better compression than Prometheus for the same retention, drop-in PromQL, and it accepts kube-prometheus-stack's dashboards and rules. Fewer turnkey pieces to assemble.
- **C. Grafana LGTM (Loki/Grafana/Mimir/Tempo)** — full observability including traces; heaviest on disk.

**Recommendation: B for metrics, plus Loki + Alloy for logs.** Note this reverses the usual
reasoning: with 512 GB of RAM, VictoriaMetrics' memory efficiency is irrelevant — it wins here
purely on **disk footprint**, which is the constrained resource (§2.2). If you'd rather have
the turnkey dashboards and alert rules of **A**, that's entirely reasonable; just cap local
retention (7–15 days) and ship anything longer to object storage.

Either way: keep Loki chunk retention short and stored locally on `proxmox-hdd` (D16 — no
in-cluster S3), and make sure metrics and logs cannot silently fill the pool — alert on ZFS
pool usage at 75%.

**Choose:** ☐ A  ☐ B  ☐ C

#### D13. PostgreSQL operator
- **A. CloudNativePG** ⭐ — the de facto standard; no external HA tooling (talks directly to the Kubernetes API); built-in continuous backup and PITR to object storage; excellent docs; CNCF project.
- **B. Zalando postgres-operator** — battle-tested at scale; Patroni-based; clunkier UX.
- **C. StackGres** — rich feature set and a web UI; more opinionated and heavier.
- **D. Percona PGO** — solid; smaller community for Postgres specifically.

**Recommendation: A.** This one isn't close.

**Choose:** ☐ A  ☐ B  ☐ C  ☐ D

#### D14. Backup & disaster recovery
Per §2 this is the *actual* resilience story. Layers, not alternatives.
- **A. Proxmox Backup Server (VM-level)** ⭐ — incremental, deduplicated, client-side encrypted; since 4.2 it writes natively to S3, so it targets B2 directly.
- **B. Velero (Kubernetes-object + PV level)** ⭐ — namespace-granular restore; the right tool for "I deleted a namespace".
- **C. CNPG-native backups to object storage** ⭐ — non-negotiable for Postgres; gives PITR.
- **D. ZFS `send`/`recv` to an off-box target** — efficient, but needs a ZFS receiver; largely made redundant by A once PBS targets B2.
- **E. Restic/Kopia from inside the cluster** — file-level; effectively what Velero's node-agent uses under the hood.

**Decided: A + B + C, all targeting Backblaze B2.** Full architecture, bucket layout, Object
Lock guidance and the B2-specific gotchas are in **§7.7**. **D** is worth adding later only if
you acquire a second ZFS box on-site.

**Choose:** ☑ **A** + ☑ **B** + ☑ **C** → Backblaze B2 *(decided 2026-08-16)*  ☐ D  ☐ E

#### D15. Repository strategy
- **A. Monorepo (this repo), three top-level directories** ⭐ — one place, atomic cross-stage changes, simplest to navigate. Flux handles multiple paths/branches fine.
- **B. Separate repos per stage** — cleaner RBAC and blast radius; more overhead for a single operator.
- **C. Monorepo + self-hosted Git (Gitea/Forgejo) mirroring to GitHub** — removes GitHub as a hard dependency for reconciliation; adds a service that itself needs backing up (and a bootstrap dependency).

**Recommendation: A for configuration** — one public monorepo across all three stages —
**plus a second private repo for secrets only**, which is a visibility split rather than a
stage split. See §8.1. This is orthogonal to A/B/C: the secrets repo exists because of
publication, not because of stage boundaries.

Consider **C** later if you want the homelab to keep reconciling with no internet access.

**Choose:** ☑ **A — public config monorepo + private secrets repo** *(decided 2026-08-16)*  ☐ B  ☐ C

#### D16. Object storage (S3) — needed by CNPG backups, Loki, Velero
- **A. Garage** ⭐ — lightweight, simple, designed for self-hosting; low RAM; actively developed.
- **B. MinIO** — ⚠️ **the upstream open-source repository was archived on 2026-02-12** ("no longer maintained") after community development ended in favour of the proprietary AIStor product; the admin console was already removed from Community Edition in mid-2025. The OpenMaxIO fork that responded to this is dormant. The live community continuation is `pgsty/minio` (AGPLv3, backports CVE patches). Still technically capable, but no longer a sensible default for a new build.
- **C. Ceph RGW** — only sensible if D7=A.
- **D. External — Backblaze B2** ⭐ — genuinely off-box, which is exactly what §2 demands for backups; ~$6–7/TB/month with free egress up to 3× stored.

**Decided: D (Backblaze B2) for all backups** *(2026-08-16)* — see §7.7. **No in-cluster S3
(A/Garage) for now** *(revised 2026-08-17)* — Loki is the only current consumer of "hot" S3,
and at this scale it's simpler to just give it a filesystem-backed PVC on `proxmox-hdd` (D12)
than to run and back up a whole extra object-storage system for it. Revisit Garage if a future
app genuinely needs an S3 API and a plain PVC won't do.

Backups must never live on the machine they're protecting — B2 is the only S3 target in this
design, and it's off-box by construction.

> **Note — object storage cannot be the *only* storage layer.** S3 serves an HTTP API, not
> block devices or POSIX filesystems, so it cannot back a PVC for Postgres, etcd or
> Prometheus. S3-backed CSI drivers (`csi-s3`, Mountpoint-S3, geesefs) mount buckets over
> FUSE with no real file locking, non-atomic renames and poor random-write performance —
> acceptable for media and write-once blobs, unsafe for databases. Object storage in this
> design is a **backup and bulk-data target**, layered on top of real block storage, never a
> replacement for it.

**Choose:** ☐ A  ☐ B  ☐ C  ☑ **D — Backblaze B2, no in-cluster S3** *(decided 2026-08-16, revised 2026-08-17)*

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Stage 0 change locks you out of the switch** | Total loss of remote access — VLAN 10 carries iDRAC, the Proxmox host *and* the switch's own management IP, so every remote path dies together | Arm the SAFETY-NET scheduled reboot before every apply (§4.8); configure the mgmt VLAN's ACL last; keep a tested USB-serial console; never run stage 0 from the `runner` LXC |
| No stateful inspection or IDS between VLANs | East-west traffic is filtered only by stateless ACLs; a compromised client can probe permitted ports freely | Accepted — the 10 Gb requirement rules out router-on-a-stick (§4.1). Trust hierarchy keeps the permit list small (§4.5); revisit only if the switch or router changes |
| OPNsense outbound NAT / static route missing after a rebuild | Every VLAN silently has no internet; looks like a switch fault | Both are in `stage0-network` and asserted by `verify.yml` (§4.4, §4.7) |
| CAPMOX API instability (`v1alpha2` → `v1alpha3`) | Breaking changes on upgrade | Pin provider versions; read release notes; never bump blind; keep a tested rollback |
| **ZFS pool fills up** | Writes fail cluster-wide; severe performance degradation past 80% | The main capacity risk on this hardware. Alert at 75%; cap Prometheus/Loki retention; conservative autoscaler `max-size`; add disks via `zpool attach` (§2.5) |
| 8K `volblocksize` left in place on RAID-Z1 | ~166% space usage — hundreds of GB wasted | Verify and set ≥16K in stage 1 (§5.1); migrate any existing volumes |
| Autoscaler exceeds host disk capacity | Pool fills; new VMs fail to start | Explicit capacity budget (§2.6); conservative `max-size`; alert on pool usage, not just memory |
| Postgres write amplification on RAID-Z1 | Poor DB performance | Assessed negligible at the actual workload (~30 tps, §2.7); revisit with a mirror vdev only if load grows materially |
| Second disk failure during an `hddpool` resilver | Pool lost, restore from B2 required | Accepted: the 4 drives are from different sources/ages (uncorrelated wear), and B2 (§7.7) already covers data loss regardless — the cost of bad luck here is restore time, not data |
| Storage replicas block scale-down | Autoscaler never scales in | Largely avoided by choosing Proxmox CSI over Longhorn/Ceph; split node pools (§6.4) regardless |
| Single host failure | Total outage | Accepted by design — off-box backups (D14) and a tested rebuild path are the answer |
| Management cluster loss | Can't reconcile stage 2 | Stage 1 recreates it from Ansible; back up its etcd and the Flux bootstrap secrets |
| Secret root key loss | Cannot decrypt anything in Git | Password manager + offline copy; document the recovery procedure |
| Talos learning curve (if D1=A) | Slow early progress | Build a throwaway cluster first; keep `talosctl` access documented |
| Gateway API learning curve | Slow stage 3 | Start with one Gateway and one HTTPRoute; migrate incrementally |

---

## 11. Implementation roadmap

Each phase should end with something demonstrably working.

| Phase | Deliverable |
|---|---|
| **0** | Scaffold the repo; create the private `homelab-secrets` repo; generate the age keypair and back it up; install the §8.1 guardrails (`.sops.yaml`, pre-commit hook, CI check, push protection) **before** making this repo public |
| **0a** | **Stage 0 by hand, from the console.** Confirm the 5130's feature set (§2.8); serial console tested; VLANs + SVIs + transit /30; OPNsense static route, outbound NAT and DHCP relay subnets; Proxmox trunk with PVID 10. *Exit: every VLAN routes, the PC sustains ≥5 Gbit/s to a VM in VLAN 20, and OPNsense's Git config backup is on.* |
| **0b** | **Stage 0 as code.** `network.yml`, the Comware template, the OPNsense role, `verify.yml`/`apply.yml`, SAFETY-NET rollback proven by deliberately breaking a config. *Exit: `verify.yml` reports zero drift, and ACLs enforce the trust hierarchy (§4.5).* Do this only once 0a is stable — you want a known-good config to render *toward*. |
| **1** | Stage 1 Ansible: Proxmox host config, **create `hddpool`**, verify `volblocksize`, VLAN-aware `vmbr0`, `netcore` LXC. *Exit: internal DNS zone resolves, both pools present, host reproducible.* |
| **2** | Stage 1 continued: `runner` LXC + web UI, `nas` LXC (SMB + NFS), secrets root. *Exit: a job runs from the UI; SMB share mounts from a desktop.* |
| **2b** | **PBS: local datastore on `hddpool` + B2 S3 datastore + sync job.** *Exit: a VM backup exists locally and in B2, and a test restore succeeds.* Done early deliberately — everything after this is recoverable. |
| **3** | Stage 1 continued: management cluster VM + Flux bootstrap. *Exit: Flux reconciles `stage2/`.* |
| **4** | Stage 2: CAPI providers + a **throwaway** workload cluster. *Exit: cluster created purely from Git.* |
| **5** | Stage 2: ClusterClass, node pools, node image pipeline. *Exit: a version bump rolls the cluster.* |
| **6** | Stage 2: cluster-autoscaler + capacity budget. *Exit: burst pool scales up and back to zero.* |
| **7** | Stage 3: Flux, CNI extras, cert-manager, both Gateways, external-dns. *Exit: a test app is reachable internally and externally with valid certs.* |
| **8** | Stage 3: Proxmox CSI (`proxmox-ssd`, `proxmox-hdd`) + static NFS PVs (`nfs-nas`). *Exit: PVCs provision on both classes and snapshot; the NFS PV mounts and is writable from a pod.* |
| **9** | Stage 3: monitoring, dashboards, alerting — including **ZFS pool usage alerts at 75%** and iDRAC hardware health. *Exit: alerts reach you.* |
| **10** | Stage 3: CloudNativePG + B2 backups (mind the §7.7 B2 quirks) + Nextcloud on `nfs-nas`. *Exit: a PITR restore into a fresh cluster succeeds.* |
| **11** | Maintenance automation, cron jobs, backup/restore drills, runbooks. *Exit: a full rebuild from Git + backups is documented and tested.* |

Phase 4 deliberately uses a throwaway cluster — expect to destroy and recreate it several
times while the Cluster API configuration settles. Don't put real data on it until phase 8.

---

## 12. Open questions

1. ~~Hardware specs~~ — answered, see §2.1. Remaining hardware details in §2.8.
2. **Confirm the SAS drives present as raw devices** — the PERC must expose them as
   non-RAID/HBA before `hddpool` can be created. It evidently already does for the SATA SSDs,
   but each drive may need explicit conversion.
3. ~~Optional: 2 more SSDs as a mirror vdev for PostgreSQL~~ — evaluated and declined: at the
   actual workload (~30 tps) the RAID-Z1 write-amplification tax is negligible (§2.5, §2.7).
4. Domain name(s), and is Cloudflare already managing the zone?
5. ~~Is there an existing router/firewall that should keep doing DHCP, or does `netcore` own it?~~
   — answered: **OPNsense keeps DHCP and the resolver**; `netcore` owns only the internal
   authoritative zone. See §4.6 and D4.
6. Do you want the internal Gateway reachable over VPN (WireGuard/Tailscale) as well as LAN?
   If so it terminates on OPNsense, and the VPN subnet needs an entry in the §4.5 trust
   hierarchy — probably at the `clients` tier, not `mgmt`.
7. **Where do the wireless APs plug in?** If they hang off the unmanaged living-room switch,
   every phone and smart plug is in VLAN 40 alongside the workstation, and IoT separation later
   requires moving them to the 5130 (§4.8). Worth deciding before running cable.
8. ~~Off-box backup target~~ — answered: **Backblaze B2**, see §7.7.
9. Any existing workloads or data to migrate, or is this greenfield? In particular, is there
   existing NAS data that needs importing into `hddpool/nas`?
10. Any GPU/transcoding requirement that needs PCIe passthrough planned in stage 1?
11. ~~D11 (secrets)~~ — answered: **SOPS + age, secrets in a private repo**, see §8.1.
12. Do you want the hardware inventory and IP plan (§2.1, §4.2) kept out of the public repo?
    See the closing note in §8.1.
13. How loud/hot is acceptable? 4 SAS HDDs in a 1U R640 add noise and heat — irrelevant in a
    rack, noticeable in a home office.

**All Tier-1 decisions are made** (D1, D2, D3, D7, D11, D14, D15, D16). Phase 0 can begin.
