#!/usr/bin/env python3
"""Serve the localhost-only, schema-aware extraction record reviewer."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from contract import build_contract
from review_store import RecordReviewStore

from histdata_pipeline.config import ProjectConfig, load_project_config
from histdata_pipeline.provenance import sha256_file, stable_hash

MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _default_run(config: ProjectConfig) -> Path:
    export_root = config.external_path("export_subdirectory", "data-extraction/exports")
    current = export_root / "current"
    if current.exists():
        return current.resolve()
    candidates = sorted(
        (path for path in export_root.glob("*") if path.is_dir() and (path / "run.json").is_file()),
        key=lambda path: path.name,
    )
    if not candidates:
        raise FileNotFoundError("No extraction run exists; run a bounded calibration/trial or pass --run")
    return candidates[-1].resolve()


def _page_payload(store: RecordReviewStore, index: int) -> dict[str, Any]:
    if not 0 <= index < len(store.pages):
        raise IndexError(index)
    model_page = store.pages[index]
    page_id = str(model_page["page_id"])
    decision = store.load(page_id)
    return {
        "index": index,
        "total": len(store.pages),
        "model": model_page,
        "review": decision,
        "review_status": decision.get("review_status", "unreviewed") if decision else "unreviewed",
        "extraction": decision.get("extraction") if decision else model_page.get("extraction"),
        "model_extraction_sha256": stable_hash(model_page.get("extraction")),
    }


def make_handler(config: ProjectConfig, store: RecordReviewStore, html: bytes) -> type[BaseHTTPRequestHandler]:
    render_root = config.external_path("render_subdirectory", "rendered-pages")

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "HistoricalRecordReview/1"

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status)

        def _index(self) -> int:
            values = parse_qs(urlparse(self.path).query)
            return int(values.get("index", ["0"])[0])

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(html)
                elif parsed.path == "/api/bootstrap":
                    review = config.table("review")
                    self._json(
                        {
                            "pages": [
                                {
                                    "page_id": page["page_id"],
                                    "document": page["pdf_relative_path"],
                                    "page": page["physical_page"],
                                    "review_status": (
                                        store.load(str(page["page_id"])).get("review_status", "unreviewed")
                                        if store.load(str(page["page_id"]))
                                        else "unreviewed"
                                    ),
                                }
                                for page in store.pages
                            ],
                            "columns": review.get("columns", []),
                            "record_list_field": review.get("record_list_field", "records"),
                            "record_schema": store.schema.model_json_schema(),
                            "progress": store.progress(),
                        }
                    )
                elif parsed.path == "/api/page":
                    self._json(_page_payload(store, self._index()))
                elif parsed.path == "/api/image":
                    page = store.pages[self._index()]
                    image = Path(str(page["render_path"])).resolve()
                    if not image.is_relative_to(render_root) or not image.is_file():
                        raise ValueError("Image path is outside the configured render root")
                    if sha256_file(image) != page["render_sha256"]:
                        raise ValueError("Rendered image hash changed")
                    body = image.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", mimetypes.guess_type(image.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._fail(HTTPStatus.NOT_FOUND, "Not found")
            except (IndexError, KeyError, OSError, TypeError, ValueError) as error:
                self._fail(HTTPStatus.BAD_REQUEST, str(error))

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if urlparse(self.path).path != "/api/save":
                self._fail(HTTPStatus.NOT_FOUND, "Not found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Review payload must be an object")
                saved = store.save(payload)
                self._json({"saved": saved, "progress": store.progress()})
            except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
                self._fail(HTTPStatus.BAD_REQUEST, str(error))

        def log_message(self, format_string: str, *args: object) -> None:
            print(f"review: {format_string % args}", file=sys.stderr)

    return ReviewHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="Immutable run directory; defaults to the complete current extraction.")
    parser.add_argument("--port", type=int, help="Local port; defaults to project.toml [review].port_records.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        config = load_project_config()
        run = config.project_path(args.run) if args.run else _default_run(config)
        run = run.resolve()
        export_root = config.external_path("export_subdirectory", "data-extraction/exports")
        if not run.is_relative_to(export_root) or not (run / "run.json").is_file():
            raise ValueError("--run must be an immutable extraction run under the configured export root")
        run_metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
        service = str(run_metadata.get("service", "flex"))
        contract = build_contract(config, service=service)
        if run_metadata.get("contract_signature") != contract.signature:
            raise ValueError("The run contract is stale relative to the current schema/prompt/settings")
        review_directory = config.checked_write_path(config.root / "manual" / "record-reviews")
        store = RecordReviewStore(run, review_directory, contract.schema)
        html = (Path(__file__).with_name("review_records.html")).read_bytes()
        port = args.port or int(config.table("review").get("port_records", 8766))
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(config, store, html))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Record reviewer: http://127.0.0.1:{port}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
