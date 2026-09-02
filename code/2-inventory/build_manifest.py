#!/usr/bin/env -S uv run
"""Build a bounded sample or complete all-physical-page manifest."""

from __future__ import annotations

import sys

from build_page_manifest import main

if __name__ == "__main__":
    raise SystemExit(main(["build", *sys.argv[1:]]))
