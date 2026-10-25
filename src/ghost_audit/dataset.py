"""Loaders for the participant-facing dataset.

Only files inside the supplied ``dataset/`` directory are read. Nothing here
touches organizer-only truth. The dataset directory is passed in explicitly so
the pipeline can run against the official copy, an extracted ``code.zip`` test
fixture, or an independently authored evaluation case.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ocr import OCRUnavailable, ocr_images

#: Evidence file suffixes that are plain UTF-8 text.
TEXT_SUFFIXES = {".log", ".txt", ".sql", ".json", ".csv"}


def parse_time(value: str) -> datetime:
    """Parse the ISO-8601 timestamps used by the dataset (always UTC)."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str

    @property
    def requested_dt(self) -> datetime:
        return parse_time(self.requested_at)

    @property
    def reported_dt(self) -> datetime:
        return parse_time(self.reported_at)

    @property
    def audit_dt(self) -> datetime:
        return parse_time(self.audit_at)


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str

    @property
    def event_dt(self) -> datetime:
        return parse_time(self.event_time)


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: str

    @property
    def captured_dt(self) -> datetime:
        return parse_time(self.captured_at)

    @property
    def kind(self) -> str:
        suffix = Path(self.file_path).suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix == ".png":
            return "png"
        if suffix == ".sql":
            return "sql"
        if suffix == ".json":
            return "json"
        if suffix == ".log":
            return "log"
        return "text"


@dataclass
class EvidenceDocument:
    """One evidence file with its extracted text (PDF/PNG are converted to text)."""

    file_id: str
    file_path: str
    origin: str
    captured_at: str
    kind: str
    text: str

    @property
    def captured_dt(self) -> datetime:
        return parse_time(self.captured_at)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class Dataset:
    """In-memory view of ``dataset/``."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"dataset directory not found: {self.root}")
        self._documents: list[EvidenceDocument] | None = None
        self._document_by_id: dict[str, EvidenceDocument] | None = None

    # -- raw tables ---------------------------------------------------------
    @property
    def deletion_requests(self) -> list[Request]:
        return [Request(**row) for row in _read_csv(self.root / "deletion_requests.csv")]

    @property
    def sample_requests(self) -> list[Request]:
        return [Request(**row) for row in _read_csv(self.root / "sample_deletion_requests.csv")]

    @property
    def users(self) -> list[UserIdentity]:
        return [UserIdentity(**row) for row in _read_csv(self.root / "user_identities.csv")]

    @property
    def audit_log(self) -> list[AuditChange]:
        return [AuditChange(**row) for row in _read_csv(self.root / "user_audit_log.csv")]

    @property
    def rules(self) -> list[Rule]:
        return [Rule(**row) for row in _read_csv(self.root / "deletion_rules.csv")]

    @property
    def activities(self) -> list[Activity]:
        return [Activity(**row) for row in _read_csv(self.root / "system_activity.csv")]

    @property
    def evidence_manifest(self) -> list[EvidenceFile]:
        return [EvidenceFile(**row) for row in _read_csv(self.root / "evidence_files.csv")]

    # -- evidence files -----------------------------------------------------
    def load_evidence_documents(self, skip_images: bool = False) -> list[EvidenceDocument]:
        """Read every evidence file and render it to text.

        SQL/JSON/log/txt files are read verbatim (parsers work on the text so a
        file's structure, not its suffix, decides how it is interpreted). PDFs
        are converted with ``pypdf``. PNGs are OCR'd with the local macOS Vision
        helper.
        """

        if self._documents is not None:
            return self._documents
        manifest = self.evidence_manifest
        image_paths: list[Path] = []
        docs: list[EvidenceDocument] = []
        for item in manifest:
            path = self.root / item.file_path
            if not path.exists():
                raise FileNotFoundError(f"manifest references a missing file: {path}")
            if item.kind == "pdf":
                text = _pdf_text(path)
            elif item.kind == "png":
                if skip_images:
                    text = ""
                else:
                    image_paths.append(path)
                    text = ""
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
            docs.append(
                EvidenceDocument(
                    file_id=item.file_id,
                    file_path=item.file_path,
                    origin=item.origin,
                    captured_at=item.captured_at,
                    kind=item.kind,
                    text=text,
                )
            )
        if image_paths:
            try:
                extracted = ocr_images(image_paths)
            except OCRUnavailable:
                if skip_images:
                    extracted = {}
                else:
                    raise
            for doc in docs:
                path = self.root / doc.file_path
                if path in extracted:
                    doc.text = extracted[path]
        self._documents = docs
        self._document_by_id = {doc.file_id: doc for doc in docs}
        return docs

    @property
    def documents(self) -> list[EvidenceDocument]:
        return self.load_evidence_documents()

    def document(self, file_id: str) -> EvidenceDocument:
        if self._document_by_id is None:
            self.load_evidence_documents()
        assert self._document_by_id is not None
        return self._document_by_id[file_id]

    # -- misc ---------------------------------------------------------------
    def read_raw(self, relative_path: str) -> str:
        return (self.root / relative_path).read_text(encoding="utf-8", errors="replace")


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("pypdf is required to read PDF evidence") from exc
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def load_json_documents(dataset: Dataset) -> list[dict]:
    """Utility for analysis scripts: manifest joined with extracted text."""

    return [
        {"file_id": doc.file_id, "path": doc.file_path, "captured_at": doc.captured_at, "text": doc.text}
        for doc in dataset.load_evidence_documents()
    ]


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
