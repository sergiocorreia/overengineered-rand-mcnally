#!/usr/bin/env -S uv run
"""Score pages using embedded text and targeted Locro fallback."""

from __future__ import annotations

import sys

from build_page_manifest import main

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not any(value == "--ocr-mode" or value.startswith("--ocr-mode=") for value in arguments):
        arguments = [*arguments, "--ocr-mode", "targeted"]
    if "--merge-existing" not in arguments:
        arguments = [*arguments, "--merge-existing"]
    raise SystemExit(main(["build", *arguments]))
