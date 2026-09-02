#!/usr/bin/env python3
"""Fail unless an extraction run and, when current, its project pointer are intact."""

import argparse
import json
import sys
from pathlib import Path

from run_integrity import verify_current, verify_run

from histdata_pipeline.config import load_project_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="Run directory; defaults to the external current symlink.")
    parser.add_argument("--allow-bounded", action="store_true", help="Verify a bounded run without requiring the current pointer.")
    args = parser.parse_args()
    try:
        config = load_project_config()
        export_root = config.external_path("export_subdirectory", "data-extraction/exports")
        run = (args.run or export_root / "current").resolve(strict=True)
        metadata = verify_run(run) if args.allow_bounded else verify_current(config, run)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps({"verified_run": str(run), "run_id": metadata["run_id"]}, sort_keys=True))


if __name__ == "__main__":
    main()
