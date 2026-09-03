#!/usr/bin/env -S uv run
"""Launch the localhost-only page review app."""

from __future__ import annotations

import argparse
import threading
import tomllib
import webbrowser
from pathlib import Path

from page_review import ReviewError, create_server

from histdata_pipeline.config import load_project_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_defaults() -> tuple[Path, Path, int]:
    path = PROJECT_ROOT / "project.toml"
    payload = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    storage = payload.get("storage", {})
    review = payload.get("review", {})
    if not isinstance(storage, dict) or not isinstance(review, dict):
        raise ValueError("project.toml [storage] and [review] must be tables")
    external = Path(str(storage.get("external_data_root", f"/home/sergio/data/{PROJECT_ROOT.name}"))).expanduser()
    if not external.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    external = external.resolve()

    def external_child(setting: str, default: str) -> Path:
        configured = Path(str(storage.get(setting, default))).expanduser()
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        path = (external / configured).resolve()
        if not path.is_relative_to(external):
            raise ValueError(f"storage.{setting} escapes external_data_root")
        return path

    mode = str(storage.get("pdf_storage", "external"))
    if mode == "project":
        local_pdf = Path(str(storage.get("local_pdf_directory", "sources/pdfs")))
        if local_pdf.is_absolute():
            raise ValueError("storage.local_pdf_directory must be project-relative")
        pdf_root = (PROJECT_ROOT / local_pdf).resolve()
        if not pdf_root.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("storage.local_pdf_directory escapes the project root")
    elif mode == "external":
        pdf_root = external_child("external_pdf_subdirectory", "pdfs")
    else:
        raise ValueError(f"Unknown storage.pdf_storage: {mode!r}")
    port = review.get("port_pages", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("project.toml review.port_pages must be an integer from 0 through 65535")
    return pdf_root, external_child("page_review_image_subdirectory", "page-review-images"), port


def _parser(pdf_root: Path, image_root: Path, port: int) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, default=PROJECT_ROOT / "data" / "pages.tsv")
    parser.add_argument("--page-overrides", type=Path, default=PROJECT_ROOT / "manual" / "page_overrides.tsv")
    parser.add_argument("--pdf-root", type=Path, default=pdf_root)
    parser.add_argument("--image-root", type=Path, default=image_root)
    parser.add_argument("--port", type=int, default=port, help=f"Local port (project default: {port}).")
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    pdf_root, image_root, port = _project_defaults()
    parser = _parser(pdf_root, image_root, port)
    args = parser.parse_args(argv)
    try:
        config = load_project_config(PROJECT_ROOT)
        page_overrides = config.checked_write_path(args.page_overrides)
        image_root = config.checked_write_path(args.image_root)
        if image_root.is_relative_to(config.root.resolve()):
            raise ValueError(f"Rendered review images must remain outside the project directory: {image_root}")
        server = create_server(
            args.pages,
            page_overrides,
            args.pdf_root,
            image_root,
            Path(__file__).with_name("review_pages.html"),
            args.port,
        )
    except (OSError, ReviewError, ValueError) as error:
        parser.error(str(error))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    state = server.store.public_state()
    progress = state["progress"]
    print(f"Review pages: {progress['resolved']}/{progress['total']} resolved")  # type: ignore[index]
    print(f"Manual decisions: {page_overrides}")
    print(f"Open {url}")
    if not args.no_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview app stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
