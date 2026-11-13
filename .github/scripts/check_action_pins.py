"""Fail when a workflow `uses:` reference is not immutable (doemefu/homelab-device-service#126).

Allowed: a local action (`./...`), `owner/repo[/path]@<40-hex commit SHA>` and
`docker://...@sha256:<64-hex>`. The workflow files are parsed as YAML, so every
`uses` key counts (step- or job-level, block or flow style, quoted or not) and
comments can neither hide nor satisfy a reference.

Usage: check_action_pins.py --self-test | check_action_pins.py FILE...
"""

import re
import sys

import yaml

ALLOWED = re.compile(r"\./\S*|[^\s@]+@[0-9a-f]{40}|docker://\S+@sha256:[0-9a-f]{64}")

SELF_TEST_REJECTED = """
jobs:
  reusable:
    uses: owner/repo/.github/workflows/x.yml@main
  steps:
    steps:
      - {name: flow style, uses: actions/checkout@v7.0.1}
      - "uses": actions/checkout@v7
      - uses: actions/checkout@v7.0.1 # uses: ./ignored
      - uses: actions/checkout@3d3c42e
      - uses: docker://alpine:3.20
"""

SELF_TEST_ACCEPTED = """
jobs:
  steps:
    steps:
      # - uses: actions/checkout@v7
      - uses: ./local-action
      - {name: flow style, uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1}
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: docker://alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
"""


def unpinned(node):
    """Yields every `uses` value below node that is not an allowed reference."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and not (isinstance(value, str) and ALLOWED.fullmatch(value)):
                yield value
            yield from unpinned(value)
    elif isinstance(node, list):
        for item in node:
            yield from unpinned(item)


def self_test():
    rejected = list(unpinned(yaml.safe_load(SELF_TEST_REJECTED)))
    accepted = list(unpinned(yaml.safe_load(SELF_TEST_ACCEPTED)))
    if len(rejected) != 6 or accepted:
        print(f"self-test failed: rejected={rejected} accepted={accepted}")
        return 1
    print("self-test passed")
    return 0


def main(paths):
    bad = 0
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for document in yaml.safe_load_all(handle):
                for value in unpinned(document):
                    print(f"::error file={path}::unpinned uses: {value}")
                    bad += 1
    if bad:
        print("Pin every `uses:` to a full commit SHA with a version comment.")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    if not sys.argv[1:]:
        sys.exit("usage: check_action_pins.py --self-test | check_action_pins.py FILE...")
    sys.exit(main(sys.argv[1:]))
