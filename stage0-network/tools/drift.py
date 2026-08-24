#!/usr/bin/env python3
"""Diff a switch's running configuration against what this repo renders.

`playbooks/verify.yml` does this against the live device; this does it against
a file, which is what you want while adopting a switch that was configured by
hand (phase 0b: "you want a known-good config to render toward"):

    ssh admin@10.100.10.2 'display current-configuration' > running.cfg
    tools/drift.py running.cfg --strict

`--strict` also lists the blocks the device has that this repo does not render.
Those are not drift — but under `configuration replace` they are exactly what
would be REMOVED, so every one of them has to be either intentional or folded
into `comware_extra_lines` before the first apply.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "filter_plugins"))
sys.path.insert(0, str(ROOT / "tools"))

import comware  # noqa: E402
import render  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("running", help="file holding `display current-configuration`")
    parser.add_argument(
        "--rendered",
        help="compare against this file instead of rendering network.yml",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also report device configuration this repo does not manage",
    )
    args = parser.parse_args()

    running = pathlib.Path(args.running).read_text()
    rendered = (
        pathlib.Path(args.rendered).read_text()
        if args.rendered
        else render.render(render.load_context({"comware_safety_net": False}))
    )

    report = comware.drift(running, rendered, strict=args.strict)
    print(comware.format_drift(report))
    return 1 if comware.has_drift(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
