#!/usr/bin/env python3
"""Render the switch configuration from network.yml, without Ansible.

Ansible renders the same template with the same variables when it applies a
config; this exists so the rendering is testable in CI, reviewable in a diff,
and reproducible on a laptop at a serial console with nothing installed.

    tools/render.py                          # print the config
    tools/render.py -o build/sw-core.cfg     # write it
    tools/render.py --safety-net             # include the break-glass job
    tools/render.py --check tests/golden/5130.cfg
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "filter_plugins"))

import netutil  # noqa: E402

NETWORK = ROOT / "group_vars" / "all" / "network.yml"
DEFAULTS = ROOT / "roles" / "comware" / "defaults" / "main.yml"
TEMPLATES = ROOT / "roles" / "comware" / "templates"
TEMPLATE = "5130.cfg.j2"

# Values Ansible pulls from the private secrets repo. Rendering offline needs
# something in their place; anything that reaches the device comes from the
# real vault, never from here.
PLACEHOLDERS = {
    "vault_comware_admin_password_hash": "hash $h$6$PLACEHOLDER",
    "vault_comware_ssh_user": "admin",
}


def load_context(overrides: dict) -> dict:
    context: dict = {}
    context.update(PLACEHOLDERS)
    context.update(yaml.safe_load(NETWORK.read_text()))
    context.update(yaml.safe_load(DEFAULTS.read_text()))
    context.update(overrides)
    return resolve(context)


def resolve(context: dict, passes: int = 3) -> dict:
    """Expand `{{ ... }}` inside the variable values themselves, at any depth.

    Ansible does this lazily; here two or three passes are enough, because the
    only nesting in the defaults is argv lists built from other variables.
    """
    env = Environment(undefined=StrictUndefined)
    env.filters.update(netutil.FILTERS)

    def expand(value):
        if isinstance(value, str) and "{{" in value:
            try:
                return env.from_string(value).render(**context)
            except Exception:  # depends on a value a later pass will fill in
                return value
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    for _ in range(passes):
        for key, value in list(context.items()):
            context[key] = expand(value)
    return context


def render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        lstrip_blocks=True,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(netutil.FILTERS)
    return env.get_template(TEMPLATE).render(**context)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", help="write to this file instead of stdout")
    parser.add_argument(
        "--safety-net",
        action="store_true",
        help="include the SAFETY-NET reboot job (what apply.yml renders)",
    )
    parser.add_argument(
        "--check",
        metavar="FILE",
        help="compare against FILE and exit non-zero if it differs",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a variable (repeatable)",
    )
    args = parser.parse_args()

    overrides = {"comware_safety_net": args.safety_net}
    for item in args.set:
        key, _, value = item.partition("=")
        overrides[key] = yaml.safe_load(value)

    config = render(load_context(overrides))

    if args.check:
        expected = pathlib.Path(args.check).read_text()
        if config != expected:
            import difflib

            sys.stdout.writelines(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    config.splitlines(keepends=True),
                    fromfile=args.check,
                    tofile="rendered",
                )
            )
            print(f"\nFAIL: rendering does not match {args.check}", file=sys.stderr)
            return 1
        print(f"OK: rendering matches {args.check}")
        return 0

    if args.out:
        pathlib.Path(args.out).write_text(config)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
