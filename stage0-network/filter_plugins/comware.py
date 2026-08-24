"""Parse Comware configuration text and diff it against a rendered config.

`display current-configuration` cannot be compared to a rendered file line by
line: the running config carries defaults we do not manage and orders sections
its own way. So this parses both into blocks and compares only the blocks we
actually render — which is the honest definition of drift for this repo.

Block model, matching Comware's own output: a line at column 0 opens a block and
the indented lines under it are its body. Bare top-level lines (`dhcp enable`,
`ip route-static ...`) become single-line blocks keyed by themselves. `#` is a
separator, not a comment, and is dropped.

Importable standalone so `tools/drift.py` and CI can use it without Ansible.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# Lines that exist only to steer the device while a config is being applied, or
# that the device never echoes back in `display current-configuration`.
_IGNORE_LINE = re.compile(r"^\s*(undo\s|return$|quit$|save\b|version\s)")

# Comware collapses runs of whitespace differently in places; normalise.
_WS = re.compile(r"\s+")


def _norm(line: str) -> str:
    return _WS.sub(" ", line.strip())


def parse(text: str) -> Dict[str, List[str]]:
    """Config text -> {block header: [body lines]}. Single-line blocks map to []."""
    blocks: Dict[str, List[str]] = {}
    current: str | None = None

    for raw in text.splitlines():
        if not raw.strip() or raw.strip() == "#":
            current = None
            continue
        if _IGNORE_LINE.match(raw):
            continue

        indented = raw[:1] in (" ", "\t")
        line = _norm(raw)

        if indented and current is not None:
            blocks[current].append(line)
        else:
            # A top-level line: opens a block, or is a whole block by itself.
            current = line
            blocks.setdefault(current, [])

    return blocks


def drift(running: str, rendered: str, strict: bool = False) -> Dict[str, list]:
    """Compare a device's running config against what this repo renders.

    Returns four buckets:
      missing_blocks   rendered blocks the device does not have at all
      missing_lines    (block, line) we render and the device lacks
      extra_lines      (block, line) the device has inside a block we manage
      unmanaged_blocks blocks the device has that we do not render
                       (reported only when strict=True — a stock switch has
                       plenty of these and they are not drift)
    """
    run = parse(running)
    ren = parse(rendered)

    missing_blocks: List[str] = []
    missing_lines: List[Tuple[str, str]] = []
    extra_lines: List[Tuple[str, str]] = []

    for header, body in ren.items():
        if header not in run:
            missing_blocks.append(header)
            continue
        have = run[header]
        for line in body:
            if line not in have:
                missing_lines.append((header, line))
        for line in have:
            if line not in body:
                extra_lines.append((header, line))

    unmanaged_blocks = [h for h in run if h not in ren] if strict else []

    return {
        "missing_blocks": missing_blocks,
        "missing_lines": missing_lines,
        "extra_lines": extra_lines,
        "unmanaged_blocks": unmanaged_blocks,
    }


def has_drift(report: Dict[str, list]) -> bool:
    return any(report[k] for k in report)


def drift_lines(report: Dict[str, list]) -> List[str]:
    """The drift report as a list, in the order you want to read it."""
    out: List[str] = []
    for header in report["missing_blocks"]:
        out.append(f"missing from device : {header}")
    for header, line in report["missing_lines"]:
        out.append(f"missing from device : {header} / {line}")
    for header, line in report["extra_lines"]:
        out.append(f"only on device      : {header} / {line}")
    for header in report["unmanaged_blocks"]:
        out.append(f"unmanaged on device : {header}")
    return out or ["no drift"]


def format_drift(report: Dict[str, list]) -> str:
    return "\n".join(drift_lines(report))


FILTERS = {
    "comware_parse": parse,
    "comware_drift": drift,
    "comware_has_drift": has_drift,
    "comware_drift_lines": drift_lines,
    "comware_format_drift": format_drift,
}


class FilterModule(object):
    def filters(self):
        return dict(FILTERS)
