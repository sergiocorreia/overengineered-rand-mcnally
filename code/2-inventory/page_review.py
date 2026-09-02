"""Local-only backend for keyboard-first review of the page manifest."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import parse_qs, urlsplit

import page_inventory

Renderer = Callable[[Path, int, Path], None]
SAFE_IMAGE = re.compile(r"^[0-9a-f]{64}/page-[0-9]{6}\.jpg$")


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    page_id: str
    expected_source_sha256: str
    classification: str
    notes: str

    def row(self) -> dict[str, str]:
        return asdict(self)


def render_page(pdf_path: Path, page: int, destination: Path) -> None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        raise ReviewError("pdftoppm is required to render review images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part.jpg")
    prefix = temporary.with_suffix("")
    try:
        subprocess.run(
            [
                executable,
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                "180",
                "-jpeg",
                "-singlefile",
                str(pdf_path),
                str(prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        temporary.replace(destination)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown render error").strip()
        raise ReviewError(f"Could not render {pdf_path.name} page {page}: {detail[-1000:]}") from error
    finally:
        temporary.unlink(missing_ok=True)


class PageReviewStore:
    """Thread-safe page state and durable, hash-pinned human decisions."""

    def __init__(
        self,
        pages_path: Path,
        overrides_path: Path,
        pdf_root: Path,
        image_root: Path,
        *,
        renderer: Renderer = render_page,
    ) -> None:
        self.pages_path = pages_path.resolve(strict=True)
        self.overrides_path = overrides_path.resolve()
        if self.overrides_path == self.pages_path:
            raise ReviewError("Manual overrides must not overwrite the generated page manifest")
        self.pdf_root = pdf_root.resolve(strict=True)
        self.image_root = image_root.resolve()
        self._renderer = renderer
        self._lock = threading.RLock()
        self.records = page_inventory.load_page_records(self.pages_path)
        if not self.records:
            raise ReviewError("Page manifest is empty")
        self.by_id = {record.page_id: record for record in self.records}
        self._decisions = self._load_decisions()

    def _load_decisions(self) -> dict[str, ReviewDecision]:
        if not self.overrides_path.exists():
            return {}
        overrides = page_inventory.load_page_overrides(self.overrides_path)
        unknown = sorted(set(overrides) - set(self.by_id))
        if unknown:
            raise ReviewError(f"Manual overrides contain unknown pages: {unknown[:3]}")
        decisions: dict[str, ReviewDecision] = {}
        for page_id, override in overrides.items():
            record = self.by_id[page_id]
            if override.expected_source_sha256 != record.source_sha256:
                raise ReviewError(f"Stale manual override for {page_id}")
            decisions[page_id] = ReviewDecision(
                page_id,
                override.expected_source_sha256,
                override.classification,
                override.notes,
            )
        return decisions

    def _write_decisions(self) -> None:
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.overrides_path.with_suffix(self.overrides_path.suffix + ".part")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=page_inventory.PAGE_OVERRIDE_FIELDS,
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(
                    self._decisions[record.page_id].row()
                    for record in self.records
                    if record.page_id in self._decisions
                )
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.overrides_path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, page_id: str, classification: str, notes: str) -> dict[str, object]:
        with self._lock:
            record = self.by_id.get(page_id)
            if record is None:
                raise ReviewError(f"Unknown page ID: {page_id}")
            if classification not in page_inventory.MANUAL_CLASSIFICATIONS:
                raise ReviewError(f"Invalid classification: {classification}")
            self._decisions[page_id] = ReviewDecision(page_id, record.source_sha256, classification, notes.strip())
            self._write_decisions()
            return self.public_state()

    def effective_type(self, record: page_inventory.PageRecord) -> str:
        decision = self._decisions.get(record.page_id)
        return decision.classification if decision else record.final_type

    def public_state(self) -> dict[str, object]:
        with self._lock:
            pages = []
            source_ids = []
            for record in self.records:
                if record.source_id not in source_ids:
                    source_ids.append(record.source_id)
                decision = self._decisions.get(record.page_id)
                pages.append(
                    {
                        "automatic_classification": record.automatic_classification,
                        "automatic_reasons": record.automatic_reasons,
                        "automatic_score": record.automatic_score,
                        "classification_source": "manual_page" if decision else record.classification_source,
                        "final_type": decision.classification if decision else record.final_type,
                        "manifest_index": record.manifest_index,
                        "manual_notes": decision.notes if decision else record.manual_notes,
                        "ocr_method": record.ocr_method,
                        "page": record.page,
                        "page_count": record.pdf_page_count,
                        "page_id": record.page_id,
                        "source_id": record.source_id,
                        "source_date": record.source_date,
                        "title": record.title,
                    }
                )
            unresolved = sum(page["final_type"] in {"unreviewed", "flagged"} for page in pages)
            return {
                "pages": pages,
                "progress": {"resolved": len(pages) - unresolved, "total": len(pages), "unresolved": unresolved},
                "source_ids": source_ids,
            }

    def image_path(self, page_id: str) -> Path:
        with self._lock:
            record = self.by_id.get(page_id)
            if record is None:
                raise ReviewError(f"Unknown page ID: {page_id}")
            relative = PurePosixPath(record.pdf_relative_path).relative_to("pdfs")
            pdf_path = (self.pdf_root / relative).resolve(strict=True)
            if not pdf_path.is_relative_to(self.pdf_root) or not pdf_path.is_file():
                raise ReviewError(f"Unsafe or missing PDF for {page_id}")
            if page_inventory.sha256_file(pdf_path) != record.source_sha256:
                raise ReviewError(f"Source PDF changed after the page manifest was built: {page_id}")
            image = self.image_root / record.source_sha256 / f"page-{record.page:06d}.jpg"
            if not image.exists():
                image.parent.mkdir(parents=True, exist_ok=True)
                self._renderer(pdf_path, record.page, image)
            with image.open("rb") as source:
                if source.read(2) != b"\xff\xd8":
                    raise ReviewError(f"Renderer did not produce a JPEG: {image}")
            return image


class ReviewServer(ThreadingHTTPServer):
    store: PageReviewStore
    html: bytes


def _json(handler: BaseHTTPRequestHandler, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/":
            body = self.server.html
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts.path == "/api/state":
            _json(self, self.server.store.public_state())
            return
        if parts.path == "/api/image":
            try:
                page_id = parse_qs(parts.query).get("page_id", [""])[0]
                image = self.server.store.image_path(page_id)
                body = image.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ReviewError) as error:
                _json(self, {"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/decision":
            _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 100_000:
                raise ReviewError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            state = self.server.store.save(
                str(payload.get("page_id", "")),
                str(payload.get("classification", "")),
                str(payload.get("notes", "")),
            )
            _json(self, state)
        except (ValueError, json.JSONDecodeError, ReviewError) as error:
            _json(self, {"error": str(error)}, HTTPStatus.BAD_REQUEST)


def create_server(
    pages_path: Path,
    overrides_path: Path,
    pdf_root: Path,
    image_root: Path,
    html_path: Path,
    port: int,
) -> ReviewServer:
    """Create a server bound only to loopback; callers may then serve it."""

    store = PageReviewStore(pages_path, overrides_path, pdf_root, image_root)
    server = cast(ReviewServer, ReviewServer(("127.0.0.1", port), ReviewHandler))
    server.store = store
    server.html = html_path.read_bytes()
    return server
