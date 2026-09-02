"""Generic FRASER title-catalog and MODS metadata adapter."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

from acquisition import SourceRecord, append_event, canonical_source_date

BASE_URL = "https://fraser.stlouisfed.org"
METADATA_URL = f"{BASE_URL}/metadata.php"
CATALOG_PATTERN = re.compile(
    r"(?:var|let|const)\s+browseByData\s*=\s*(\{.*?\}|\[.*?\])\s*;\s*(?=(?:var|let|const)\s+browse|</script>)",
    re.DOTALL,
)
MODS = {"mods": "http://www.loc.gov/mods/v3"}


@dataclass(frozen=True, slots=True)
class CatalogItem:
    catalog_order: int
    item_id: str
    name: str
    url: str
    decade: int | None
    sort_order: str


@dataclass(frozen=True, slots=True)
class ItemMetadata:
    catalog_order: int
    item_id: str
    sort_date: str
    date_issued: str
    title: str
    subtitle: str
    extent: str
    item_url: str
    pdf_url: str
    filename: str


def _catalog_rows(payload: object) -> list[dict[str, object]]:
    """Normalize FRASER's observed list, sectioned-dict, and top-level-items variants."""

    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        values = payload["items"]
    elif isinstance(payload, dict):
        values = []
        for section in payload.values():
            if isinstance(section, dict) and isinstance(section.get("items"), list):
                values.extend(section["items"])
            elif isinstance(section, list):
                values.extend(section)
    else:
        values = []
    if not all(isinstance(value, dict) for value in values):
        raise ValueError("FRASER catalog contains a malformed item")
    return values  # type: ignore[return-value]


def parse_catalog(html: str) -> list[CatalogItem]:
    match = CATALOG_PATTERN.search(html)
    if match is None:
        raise ValueError("FRASER catalog data were not found in the title page")
    rows = _catalog_rows(json.loads(match.group(1)))
    items: list[CatalogItem] = []
    seen: set[str] = set()
    for row in rows:
        item_id = str(row.get("id", "")).strip()
        name = str(row.get("name", "")).strip()
        url = str(row.get("url", "")).strip()
        if not item_id or not name or not url:
            raise ValueError("FRASER catalog item is missing id, name, or url")
        if item_id in seen:
            raise ValueError(f"Duplicate FRASER catalog item ID: {item_id}")
        seen.add(item_id)
        decade_raw = row.get("decade")
        items.append(
            CatalogItem(
                catalog_order=len(items) + 1,
                item_id=item_id,
                name=name,
                url=url,
                decade=int(decade_raw) if decade_raw not in (None, "") else None,
                sort_order=str(row.get("sortOrder", row.get("sort_order", ""))),
            )
        )
    if not items:
        raise ValueError("FRASER catalog contained no items")
    return items


def _text(parent: ElementTree.Element, path: str) -> str:
    element = parent.find(path, MODS)
    return "" if element is None or element.text is None else element.text.strip()


def source_filename(pdf_url: str) -> str:
    if not pdf_url:
        return ""
    filename = unquote(Path(urlparse(pdf_url).path).name)
    if Path(filename).name != filename or Path(filename).suffix.casefold() != ".pdf":
        raise ValueError(f"Invalid FRASER PDF filename: {pdf_url}")
    return filename


def parse_item_metadata(xml: str, item: CatalogItem) -> ItemMetadata:
    root = ElementTree.fromstring(xml)
    metadata = root if root.tag.endswith("mods") else root.find(".//mods:mods", MODS)
    if metadata is None:
        raise ValueError(f"MODS metadata not found for FRASER item {item.item_id}")
    identifier = _text(metadata, "mods:recordInfo/mods:recordIdentifier")
    if identifier != item.item_id:
        raise ValueError(f"FRASER item ID mismatch: expected {item.item_id}, got {identifier or 'blank'}")
    pdf_url = _text(metadata, "mods:location/mods:pdfUrl")
    return ItemMetadata(
        catalog_order=item.catalog_order,
        item_id=item.item_id,
        sort_date=_text(metadata, "mods:originInfo/mods:sortDate"),
        date_issued=_text(metadata, "mods:originInfo/mods:dateIssued"),
        title=_text(metadata, "mods:titleInfo/mods:title") or item.name,
        subtitle=_text(metadata, "mods:titleInfo/mods:subTitle"),
        extent=_text(metadata, "mods:physicalDescription/mods:extent"),
        item_url=_text(metadata, "mods:location/mods:url") or urljoin(BASE_URL, item.url),
        pdf_url=pdf_url,
        filename=source_filename(pdf_url),
    )


def append_metadata(path: Path, record: ItemMetadata) -> None:
    append_event(path, asdict(record))


def load_metadata(path: Path) -> dict[str, ItemMetadata]:
    if not path.exists():
        return {}
    records: dict[str, ItemMetadata] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                record = ItemMetadata(**json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"Invalid FRASER metadata at {path}:{line_number}: {error}") from error
            records[record.item_id] = record
    return records


def to_source_record(metadata: ItemMetadata, *, source_order: int, source_id_prefix: str = "fraser") -> SourceRecord:
    if not metadata.pdf_url or not metadata.filename:
        raise ValueError(f"FRASER item {metadata.item_id} has no downloadable PDF")
    return SourceRecord(
        source_order=source_order,
        source_id=f"{source_id_prefix}_{metadata.item_id}".casefold().replace("-", "_"),
        provider="FRASER",
        provider_id=metadata.item_id,
        title=" — ".join(part for part in (metadata.title, metadata.subtitle) if part),
        source_date=canonical_source_date(metadata.sort_date, context=f"FRASER item {metadata.item_id} sortDate"),
        item_url=metadata.item_url,
        download_url=metadata.pdf_url,
        acquisition_method="direct",
        filename=metadata.filename,
        expected_sha256="",
        min_pages=1,
        max_pages=None,
        notes=f"date_issued={metadata.date_issued}; sort_date={metadata.sort_date}; extent={metadata.extent}",
    )
