#!/usr/bin/env -S uv run
"""Apply the fail-closed readiness gate and export reviewed selected pages."""

from __future__ import annotations

import sys

from build_page_manifest import main

if __name__ == "__main__":
    raise SystemExit(main(["gate", *sys.argv[1:]]))
