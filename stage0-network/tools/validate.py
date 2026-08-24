#!/usr/bin/env python3
"""Check network.yml against the invariants the rest of the repo relies on.

network.yml is consumed by four stages, and most of the ways it can be wrong
are silent: an address inside the DHCP pool that stage 2 also assigns
statically, a policy rule naming a VLAN that no longer exists, a trunk that
does not permit its own PVID. Everything here is a rule that, if broken, would
show up much later as something that looks like a hardware fault.

    tools/validate.py            # exits non-zero on the first problem found
"""

from __future__ import annotations

import ipaddress
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NETWORK = ROOT / "group_vars" / "all" / "network.yml"

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def net(subnet: str) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(subnet, strict=False)


def addr(address: str) -> ipaddress.IPv4Address:
    return ipaddress.ip_address(address)


def pool_range(pool: dict) -> tuple:
    return addr(pool["start"]), addr(pool["end"])


def in_pool(address, pool: dict) -> bool:
    start, end = pool_range(pool)
    return start <= addr(address) <= end


def pools_overlap(a: dict, b: dict) -> bool:
    a_start, a_end = pool_range(a)
    b_start, b_end = pool_range(b)
    return a_start <= b_end and b_start <= a_end


def main() -> int:
    model = yaml.safe_load(NETWORK.read_text())
    vlans = model["vlans"]
    by_name = {v["name"]: v for v in vlans}
    supernet = net(model["supernet"])

    # --- VLANs -------------------------------------------------------------
    for key in ("id", "name", "subnet"):
        seen = [v[key] for v in vlans]
        duplicates = {x for x in seen if seen.count(x) > 1}
        if duplicates:
            fail(f"duplicate VLAN {key}: {sorted(duplicates)}")

    for vlan in vlans:
        subnet = net(vlan["subnet"])
        label = f"VLAN {vlan['id']} ({vlan['name']})"

        if not subnet.subnet_of(supernet):
            fail(f"{label}: {subnet} is outside the supernet {supernet}")
        if addr(vlan["svi"]) not in subnet:
            fail(f"{label}: SVI {vlan['svi']} is not in {subnet}")
        if vlan["id"] == model["quarantine_vlan"]:
            fail(f"{label}: the quarantine VLAN must have no SVI and no route")

        # VLAN ID == third octet is the convention the whole plan reads by.
        third_octet = int(str(subnet.network_address).split(".")[2])
        if third_octet != vlan["id"]:
            fail(f"{label}: third octet {third_octet} does not match the VLAN id")

        pools = {k: vlan[k] for k in ("dhcp_pool", "lb_pool") if k in vlan}
        for name, pool in pools.items():
            start, end = pool_range(pool)
            if start > end:
                fail(f"{label}: {name} starts after it ends")
            for edge in (pool["start"], pool["end"]):
                if addr(edge) not in subnet:
                    fail(f"{label}: {name} address {edge} is outside {subnet}")
            if in_pool(vlan["svi"], pool):
                fail(f"{label}: the SVI {vlan['svi']} sits inside {name}")

        if "dhcp_pool" in pools and "lb_pool" in pools:
            if pools_overlap(pools["dhcp_pool"], pools["lb_pool"]):
                fail(f"{label}: the DHCP pool and the LB-IPAM pool overlap")

        if vlan["dhcp"] and "dhcp_pool" not in vlan:
            fail(f"{label}: dhcp is true but no dhcp_pool is defined for Kea to serve")
        if not vlan["dhcp"] and "dhcp_pool" in vlan:
            fail(f"{label}: has a dhcp_pool but dhcp is false, so nothing relays to it")

        # Statically assigned addresses (stage 2 writes these into
        # talconfig.yaml) must not be handed out by anything else.
        for role, addresses in (vlan.get("allocations") or {}).items():
            for address in addresses:
                if addr(address) not in subnet:
                    fail(f"{label}: {role} address {address} is outside {subnet}")
                for name, pool in pools.items():
                    if in_pool(address, pool):
                        fail(f"{label}: {role} address {address} sits inside {name}")

    # Overlapping VLAN subnets, and the VPN against all of them.
    all_subnets = [(v["name"], net(v["subnet"])) for v in vlans]
    all_subnets.append(("vpn", net(model["vpn"]["subnet"])))
    for i, (name_a, a) in enumerate(all_subnets):
        for name_b, b in all_subnets[i + 1 :]:
            if a.overlaps(b):
                fail(f"{name_a} {a} overlaps {name_b} {b}")

    # --- named hosts -------------------------------------------------------
    seen_ips: dict[str, str] = {}
    for name, host in model["infra_hosts"].items():
        vlan = next((v for v in vlans if v["id"] == host["vlan"]), None)
        if vlan is None:
            fail(f"host {name}: VLAN {host['vlan']} does not exist")
            continue
        if addr(host["ip"]) not in net(vlan["subnet"]):
            fail(f"host {name}: {host['ip']} is not in VLAN {vlan['id']}'s subnet")
        if host["ip"] in seen_ips:
            fail(f"host {name}: {host['ip']} is already used by {seen_ips[host['ip']]}")
        seen_ips[host["ip"]] = name
        for pool_name in ("dhcp_pool", "lb_pool"):
            if pool_name in vlan and in_pool(host["ip"], vlan[pool_name]):
                fail(f"host {name}: {host['ip']} sits inside VLAN {vlan['id']}'s {pool_name}")

    gateway = model["default_gateway"]
    if gateway != model["infra_hosts"]["opnsense"]["ip"]:
        fail(f"default_gateway {gateway} is not the OPNsense transit address")

    # --- policy ------------------------------------------------------------
    for rule in model["policy"]:
        source, destination = rule["from"], rule["to"]
        unknown = [side for side in (source, destination) if side not in by_name]
        if unknown:
            fail(f"policy {source}->{destination}: no VLAN named {', '.join(unknown)}")
            continue
        if by_name[source].get("trusted"):
            fail(
                f"policy {source}->{destination}: {source} is a trusted tier, which "
                "renders no ACL at all - untrust it or drop the rule"
            )
        if destination == "transit":
            fail("policy: never filter the transit VLAN - it carries all return traffic")
        for permit in rule.get("except", []):
            if permit["host"] not in model["infra_hosts"]:
                fail(f"policy {source}->{destination}: unknown host {permit['host']}")
            elif model["infra_hosts"][permit["host"]]["vlan"] != by_name[destination]["id"]:
                fail(
                    f"policy {source}->{destination}: {permit['host']} is not in "
                    f"{destination}, so this permit does not do what it says"
                )
            if permit["proto"] not in ("tcp", "udp"):
                fail(f"policy {source}->{destination}: unsupported protocol {permit['proto']}")
            if not 1 <= int(permit["port"]) <= 65535:
                fail(f"policy {source}->{destination}: port {permit['port']} out of range")

    for entry in model["internet_egress"]:
        if entry["vlan"] not in by_name:
            fail(f"internet_egress: no VLAN named {entry['vlan']}")

    # --- ports -------------------------------------------------------------
    ids = {v["id"] for v in vlans} | {model["quarantine_vlan"]}
    names = [p["name"] for p in model["ports"]]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        fail(f"the same port is configured twice: {sorted(duplicates)}")

    for port in model["ports"]:
        if port["type"] == "access":
            if port["vlan"] not in ids:
                fail(f"port {port['name']}: access VLAN {port['vlan']} does not exist")
        elif port["type"] == "trunk":
            if port["pvid"] not in ids:
                fail(f"port {port['name']}: PVID {port['pvid']} does not exist")
            if port["pvid"] not in port["permit"]:
                fail(f"port {port['name']}: PVID {port['pvid']} is not in the permit list")
            if model["quarantine_vlan"] in port["permit"]:
                fail(f"port {port['name']}: the quarantine VLAN must not be trunked")
        else:
            fail(f"port {port['name']}: unknown type {port['type']}")

    # --- the rendered config ----------------------------------------------
    # Comware is unhappy with non-ASCII in descriptions and comments, and the
    # failure is a rejected line in the middle of an apply.
    sys.path.insert(0, str(ROOT / "tools"))
    import render  # noqa: E402

    config = render.render(render.load_context({"comware_safety_net": False}))
    for number, line in enumerate(config.splitlines(), start=1):
        if not line.isascii():
            fail(f"rendered config line {number} is not ASCII: {line.strip()}")

    if errors:
        print("network.yml is not internally consistent:\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"network.yml is consistent: {len(vlans)} VLANs, "
          f"{len(model['infra_hosts'])} named hosts, {len(model['policy'])} policy rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
