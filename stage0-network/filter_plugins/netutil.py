"""Small IP helpers, in stdlib only.

These are Ansible filter plugins, but the module is deliberately importable on
its own so that `tools/render.py` can render the switch template with nothing
installed but Python — which is what makes the template testable in CI, and at
a serial console, without Ansible or a switch.
"""

from __future__ import annotations

import ipaddress


def netmask(subnet: str) -> str:
    """10.100.10.0/24 -> 255.255.255.0"""
    return str(ipaddress.ip_network(subnet, strict=False).netmask)


def wildcard(subnet: str) -> str:
    """10.100.10.0/24 -> 0.0.0.255   (Comware ACLs take an inverse mask)"""
    return str(ipaddress.ip_network(subnet, strict=False).hostmask)


def network(subnet: str) -> str:
    """10.100.10.0/24 -> 10.100.10.0"""
    return str(ipaddress.ip_network(subnet, strict=False).network_address)


def prefixlen(subnet: str) -> int:
    return ipaddress.ip_network(subnet, strict=False).prefixlen


def in_subnet(address: str, subnet: str) -> bool:
    return ipaddress.ip_address(address) in ipaddress.ip_network(subnet, strict=False)


def in_range(address: str, start: str, end: str) -> bool:
    return (
        ipaddress.ip_address(start)
        <= ipaddress.ip_address(address)
        <= ipaddress.ip_address(end)
    )


def usable_hosts(subnet: str) -> int:
    net = ipaddress.ip_network(subnet, strict=False)
    return max(net.num_addresses - 2, 0)


FILTERS = {
    "netmask": netmask,
    "wildcard": wildcard,
    "network": network,
    "prefixlen": prefixlen,
    "in_subnet": in_subnet,
    "in_range": in_range,
    "usable_hosts": usable_hosts,
}


class FilterModule(object):
    def filters(self):
        return dict(FILTERS)
