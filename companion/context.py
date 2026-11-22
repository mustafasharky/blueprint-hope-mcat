"""One-time history imports and reusable context uploads, with explicit revisions."""

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from companion.models import Target
from companion.store import Conflict
from study_sync import digest, timestamp


def read_file(path):
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError("Context upload limit is 5 MB; split large history files")
    if path.suffix.lower() in {".txt", ".md", ".csv"}:
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".docx":
        with ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 5_000_000:
                raise ValueError("Expanded document is too large")
            root = ET.fromstring(archive.read(info))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
    raise ValueError(
        "Upload UTF-8 text, Markdown, CSV, or DOCX; export Google Docs as one of these formats"
    )


def upload(store, document_id, title, text, expected=0, entries=None):
    if (
        not 1 <= len(document_id) <= 150
        or not 1 <= len(title) <= 300
        or not 1 <= len(text) <= 500000
    ):
        raise ValueError("Invalid context identity, title, or text size")
    entries = entries or []
    if len(entries) > 500:
        raise ValueError("At most 500 structured entries per upload")
    old = store.get("context_document", document_id)
    if old and old["content_hash"] == digest([text, entries]):
        return {
            "saved": True,
            "document_id": document_id,
            "version": old["version"],
            "status": "unchanged",
        }
    if (old["version"] if old else 0) != expected:
        raise Conflict("Context changed; refresh its version before uploading an edit")
    seen = set()
    normalized = []
    for raw in entries:
        allowed = {
            "id",
            "text",
            "occurred_at",
            "timestamp_precision",
            "reason",
            "target",
            "attempt_id",
            "link_confirmed",
            "help_timing",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) - allowed
            or not {"id", "text"} <= set(raw)
        ):
            raise ValueError("Invalid structured context entry")
        if (
            not isinstance(raw["id"], str)
            or not 1 <= len(raw["id"]) <= 150
            or raw["id"] in seen
        ):
            raise ValueError(
                "Entries need unique stable IDs; paragraph positions are insufficient"
            )
        seen.add(raw["id"])
        row = dict(raw)
        if not isinstance(row["text"], str) or not 1 <= len(row["text"]) <= 20000:
            raise ValueError("Invalid entry text")
        precision = row.get("timestamp_precision", "unknown")
        if precision not in {"exact", "day", "unknown"}:
            raise ValueError("Use exact, day, or unknown timestamp precision")
        if row.get("occurred_at"):
            if precision == "day":
                from datetime import date

                date.fromisoformat(row["occurred_at"])
            elif precision == "exact":
                timestamp(row["occurred_at"])
            else:
                raise ValueError("Provide timestamp precision with an occurrence date")
        if row.get("target"):
            row["target"] = Target.model_validate(row["target"]).model_dump()
        if row.get("reason") not in {
            None,
            "did_not_know",
            "forgot",
            "confused",
            "verifying",
            "curiosity",
            "other",
        }:
            raise ValueError("Unsupported lookup reason")
        if row.get("attempt_id") and not store.get("attempts", row["attempt_id"]):
            raise ValueError("Unknown linked attempt")
        if "link_confirmed" in row and type(row["link_confirmed"]) is not bool:
            raise ValueError("link_confirmed must be an explicit boolean")
        if row.get("help_timing") not in {
            None,
            "before_answer",
            "after_answer",
            "during_review",
            "unknown",
        }:
            raise ValueError("Invalid help timing")
        row.update(
            timestamp_precision=precision,
            document_id=document_id,
            factual_resolution="unverified",
            learning_verification="unknown",
            link_status="confirmed"
            if row.get("link_confirmed") is True
            else "candidate"
            if row.get("attempt_id")
            else "unlinked",
        )
        normalized.append(row)
    doc = store.put(
        "context_document",
        document_id,
        {
            "title": title,
            "text": text,
            "content_hash": digest([text, entries]),
            "source": "learner_upload",
            "entry_ids": [r["id"] for r in normalized],
            "instruction_status": "untrusted_source_data",
        },
        expected=expected,
    )
    for row in normalized:
        identifier = document_id + ":" + row["id"]
        store.put(
            "context_entry", identifier, {**row, "document_version": doc["version"]}
        )
    return {
        "saved": True,
        "document_id": document_id,
        "version": doc["version"],
        "entries": len(normalized),
        "link_policy": "Candidate links never reclassify help or novelty.",
    }
