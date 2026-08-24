#!/usr/bin/env python3
"""Tests for the stage 0 model, renderer and drift check.

Deliberately stdlib-only and hardware-free: this runs in CI, and it is the
only thing that can tell you the switch config is wrong *before* you apply it
to the switch you are reaching the switch through.

    tests/test_stage0.py
"""

from __future__ import annotations

import copy
import importlib
import pathlib
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "filter_plugins"))

import comware  # noqa: E402
import netutil  # noqa: E402
import render  # noqa: E402
import validate  # noqa: E402

GOLDEN = ROOT / "tests" / "golden" / "5130.cfg"
MODEL = yaml.safe_load((ROOT / "group_vars" / "all" / "network.yml").read_text())

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"{'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def validate_model(model: dict) -> bool:
    """Run the validator over a modified model. True when it accepts it."""
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        yaml.safe_dump(model, handle)
        path = pathlib.Path(handle.name)
    module = importlib.reload(validate)
    module.NETWORK = path
    try:
        return module.main() == 0
    finally:
        path.unlink()


def mutate(**changes):
    """A copy of the real model with one thing broken."""
    model = copy.deepcopy(MODEL)
    for path, value in changes.items():
        target = model
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
    return model


# --- the model itself -------------------------------------------------------

check(validate_model(copy.deepcopy(MODEL)), "the committed network.yml validates")

servers = next(i for i, v in enumerate(MODEL["vlans"]) if v["name"] == "servers")

check(
    not validate_model(mutate(**{f"vlans.{servers}.svi": "10.100.99.9"})),
    "an SVI outside its own subnet is rejected",
)
check(
    not validate_model(
        mutate(**{f"vlans.{servers}.allocations": {"control_plane": ["10.100.20.150"]}})
    ),
    "a static node address inside the DHCP pool is rejected",
)
check(
    not validate_model(
        mutate(**{f"vlans.{servers}.lb_pool": {"start": "10.100.20.150", "end": "10.100.20.250"}})
    ),
    "an LB-IPAM pool overlapping the DHCP pool is rejected",
)
check(
    not validate_model(mutate(**{"infra_hosts.pve.ip": "10.100.20.41"})),
    "a named host in the wrong VLAN is rejected",
)
check(
    not validate_model(mutate(**{"policy.0.to": "nonexistent"})),
    "a policy rule naming a VLAN that does not exist is rejected",
)
check(
    not validate_model(mutate(**{"policy.0.from": "clients"})),
    "a policy rule sourced from a trusted tier is rejected",
)
check(
    not validate_model(mutate(**{"ports.2.pvid": 40})),
    "a trunk whose PVID is not in its permit list is rejected",
)
check(
    not validate_model(mutate(**{"default_gateway": "10.100.99.9"})),
    "a default gateway that is not the router is rejected",
)

# --- rendering --------------------------------------------------------------

config = render.render(render.load_context({"comware_safety_net": False}))
check(config == GOLDEN.read_text(), "rendering matches tests/golden/5130.cfg")

# The four permits the cluster genuinely needs (plan 4.5), and the deny that
# makes them meaningful.
for needed in [
    "rule 10 permit tcp destination 10.100.10.41 0 destination-port eq 8006",
    "rule 20 permit udp destination 10.100.10.10 0 destination-port eq dns",
    "rule 40 permit tcp destination 10.100.10.10 0 destination-port eq 8081",
    "rule 50 permit tcp destination 10.100.10.41 0 destination-port eq 9221",
    "rule 60 permit tcp destination 10.100.10.40 0 destination-port eq 443",
    "rule 70 deny ip destination 10.100.10.0 0.0.0.255",
    "rule 1000 permit ip",
]:
    check(f" {needed}" in config, f"ACL contains: {needed}")

check(
    " rule 5 permit udp destination-port eq bootps" in config,
    "every relayed VLAN's ACL permits DHCP (plan 4.5 note 1)",
)
check(
    "packet-filter" not in config.split("interface Vlan-interface99")[1].split("#")[0],
    "the transit interface carries no packet-filter",
)
check(
    "acl advanced 3010" not in config and "acl advanced 3040" not in config,
    "trusted tiers render no ACL at all",
)
check(
    "port trunk permit vlan 10 20 50" in config,
    "VLAN 50 is pre-permitted on the trunk, so enabling IoT needs no port change",
)
check("scheduler schedule" not in config, "the safety net is not in the steady-state config")
check(
    "scheduler schedule SAFETY-NET"
    in render.render(render.load_context({"comware_safety_net": True})),
    "the safety net is rendered when apply.yml asks for it",
)
check(config.isascii(), "the rendered config is pure ASCII")

# --- drift ------------------------------------------------------------------

check(not comware.has_drift(comware.drift(config, config)), "identical configs show no drift")

hand_edited = config.replace(
    " rule 70 deny ip destination 10.100.10.0 0.0.0.255",
    " rule 70 permit ip destination 10.100.10.0 0.0.0.255",
)
report = comware.drift(hand_edited, config)
check(
    any("rule 70 deny" in line for _, line in report["missing_lines"]),
    "a rule changed by hand on the device is reported as drift",
)
check(
    comware.has_drift(comware.drift(config.replace("acl advanced 3020", "acl advanced 3021"), config)),
    "a missing ACL is reported as drift",
)
check(
    not comware.has_drift(
        comware.drift(config + "\n#\nsflow agent ip 10.100.10.2\n#\n", config)
    ),
    "configuration this repo does not manage is not drift by default",
)
check(
    comware.has_drift(
        comware.drift(config + "\n#\nsflow agent ip 10.100.10.2\n#\n", config, strict=True)
    ),
    "--strict reports unmanaged configuration too",
)

# --- helpers ----------------------------------------------------------------

check(netutil.wildcard("10.100.20.0/24") == "0.0.0.255", "wildcard masks are inverse masks")
check(netutil.netmask("10.100.99.0/30") == "255.255.255.252", "the transit /30 renders correctly")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for failure in failures:
        print(f"  - {failure}")
    raise SystemExit(1)
print("all checks passed")
