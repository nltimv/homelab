# Stage 0 — network foundation

Ansible for the two devices every other stage depends on: the **HPE 5130** core
switch and the **OPNsense** router. See [`docs/PLAN.md` §4](../docs/PLAN.md) for
the design and the reasoning; this file is how you run it.

Two rules, both load-bearing:

1. **Stage 0 is never GitOps.** A reconciler that manages the network it depends
   on cannot roll back its own mistakes: push a bad ACL, lose contact with the
   switch, and the controller that would fix it can no longer reach it. Every
   change here is triggered by hand.
2. **Run it from the workstation, never from the `runner` LXC.** The runner sits
   behind the switch it would be reconfiguring, on a host whose management VLAN
   it might be changing.

## Layout

```
group_vars/all/network.yml     the ONLY place VLANs, subnets and policy are written
inventory.yml                  two devices, both addressed from network.yml
roles/comware/                 renders the full switch config, scp + configuration replace
roles/opnsense/                REST API: route, outbound NAT, filter rules, Unbound, Kea, WireGuard
playbooks/validate.yml         model self-consistency, no device contact
playbooks/render.yml           render the switch config into build/, no device contact
playbooks/verify.yml           read-only drift check on both devices
playbooks/apply.yml            converge both devices (explicit confirmation required)
tools/                         the same rendering, validation and drift check without Ansible
tests/                         hardware-free tests of all of the above
```

Everything renders from `network.yml`. Without it the VLAN model would be
hand-maintained in at least five places — 5130 SVIs and ACLs, OPNsense
route/NAT/rules, Kea subnets, Proxmox `bridge-vids` and VM tags, and Cilium's
LB-IPAM pool — and they would drift. Later stages read the same file: stage 1
for `bridge-vids`, stage 2 for node addresses that must sit outside the DHCP and
LB pools, stage 3 for the `CiliumLoadBalancerIPPool`.

## Prerequisites

Stage 0-as-code assumes phase 0a is done and stable — the network is up, and
you have a known-good config to render *toward*.

**On the switch** (once, from the serial console):

```
 ssh server enable
 scp server enable
 local-user <user> class manage
  service-type ssh terminal
  authorization-attribute user-role network-admin
```

**On the router** (once, in the UI): interface assignment, the transit `/30`,
the gateway object pointing at `10.100.99.2`, and an API key under
*System → Access → Users*. Automating this part is where a weekend disappears
and it is exactly the part you need working before Ansible can talk to the box.
While you are there, turn on *System → Configuration → Backups → Git*: it
commits `config.xml` on every change and is how you notice the change made in
the UI at 1am that these playbooks do not know about.

**On the workstation:**

```sh
ansible-galaxy collection install -r requirements.yml
ln -s ~/src/homelab-secrets/stage0-network/vault.sops.yml group_vars/all/vault.sops.yml
```

The vault supplies `vault_comware_ssh_user`, `vault_comware_admin_password_hash`
(the *hashed* form, exactly as `display current-configuration` prints it),
`vault_opnsense_api_key` and `vault_opnsense_api_secret`. It is decrypted in
place by the `community.sops` vars plugin, so no plaintext copy is ever written
to disk, and `.gitignore` keeps the symlink itself out of this repo.

## Running it

```sh
ansible-playbook playbooks/validate.yml            # the model, offline
ansible-playbook playbooks/render.yml              # the switch config, offline
ansible-playbook playbooks/verify.yml              # drift, read-only
ansible-playbook playbooks/apply.yml -e confirm=apply
ansible-playbook playbooks/apply.yml -e confirm=apply --limit switches
```

Without Ansible — at a serial console, or in CI:

```sh
tools/validate.py                 # network.yml against its own invariants
tools/render.py -o /tmp/5130.cfg  # the same file Ansible would render
tools/drift.py running.cfg        # diff a captured running config against it
tests/test_stage0.py              # all of the above, plus the drift check itself
```

## What `apply.yml` does to the switch, and why in that order

`configuration replace` converges the device *exactly*: anything not in the
rendered file is removed from it. That is the point — it is what makes drift
meaningful — and it is also how you lock yourself out. So:

1. **`save`** — the safety net reboots to the *saved* config, so the saved
   config must be the known-good one currently running.
2. **arm** — the SAFETY-NET reboot job is rendered *into* the config being
   applied, so it exists from the instant the risky change lands. It is not in
   the saved config, so the reboot removes it.
3. **apply** — `scp` to `flash:` then `configuration replace`.
4. **prove** — reachability, then a full drift check.
5. **disarm and save** — only once step 4 passed. If contact was lost instead,
   nobody disarms anything and the switch reboots itself back within ten
   minutes.

Set `comware_apply_method: merge` to feed the same file through the CLI instead.
It only adds and updates, never removes — the right choice for the first few
applies, and the fallback if `configuration replace file` turns out not to be on
your firmware.

## Adopting a switch that was configured by hand

The rendered file is the *whole* configuration, so before the first
`replace`-mode apply, find everything on the device that this repo does not
model:

```sh
ssh admin@10.100.10.2 'display current-configuration' > running.cfg
tools/drift.py running.cfg --strict
```

Lines under `only on device` and `unmanaged on device` are what `replace` would
delete. Each one is a decision: fold it into `network.yml` if it is part of the
model, into `comware_extra_lines` (or `comware_global_lines`) if it is
device-specific — SSH host keys, STP tuning, SNMP, logging hosts — or let it go
if it was never wanted. Iterate until `--strict` is quiet, and only then apply.

## Break-glass

- **Keep a USB-serial adapter within reach of the switch.** VLAN 10 carries
  iDRAC *and* the Proxmox host *and* the switch's own management address, so a
  bad VLAN 10 change removes every remote path simultaneously. Serial to the
  5130 and physical console on OPNsense are the only true fallbacks.
- The SAFETY-NET job reboots the switch ten minutes after an apply unless
  `apply.yml` disarms it. Adjust with `comware_safety_net_delay`.
- Prove it works before you need it: apply a config with a deliberately broken
  VLAN 10 ACL and let the reboot recover it. That is a stage 0 exit criterion.
- OPNsense has no equivalent. Its config history (*System → Configuration →
  History*) and the Git backup are the rollback path, both reached from the
  console if the LAN side is broken.

## What this does not manage

- **The OPNsense bootstrap** — interface assignment, the LAN/transit address,
  the gateway object, enabling the API. One-time, by hand, on purpose.
- **DHCP reservations and household specifics in Kea.** The relayed subnets are
  created if missing; an existing subnet is reported, never rewritten, because
  Kea also holds things this repo does not model.
- **VLAN 50 (IoT/guest).** Deferred (§4.8) and commented out in `network.yml`,
  but already permitted on the trunk so enabling it needs no port change.
- **Suricata, aliases and any north-south policy beyond per-VLAN egress.**

## Confirm against your own firmware

These are the assumptions this code makes about hardware it cannot ask
(`docs/PLAN.md` §12):

| # | Assumption | Where it bites |
|---|---|---|
| 1 | The 5130 is the **EI** feature set — SVI addressing, `packet-filter`, `dhcp select relay` | Everything |
| 2 | `configuration replace file` exists | `comware_apply_method: replace` — fall back to `merge` |
| 3 | The `scheduler job` / `scheduler schedule` syntax matches | The safety net, which you will not notice is wrong until you need it |
| 4 | `ansibleguy.opnsense` covers what the router role uses, and the Kea API paths are current | `roles/opnsense` |
| 5 | Comware echoes well-known ports by name (`dns`, `domain`, `bootps`) | Harmless but noisy: every verify would report drift. Fix the maps in `roles/comware/defaults/main.yml` |
| 6 | `scp -O` (legacy protocol) works against the switch | Copying the config to `flash:` |
