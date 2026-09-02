#!/usr/bin/env -S uv run
"""Public wrapper for bounded FRASER catalog-to-source-manifest acquisition."""

from fraser_adapter import main

if __name__ == "__main__":
    raise SystemExit(main())
