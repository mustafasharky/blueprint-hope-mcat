"""Allowlisted capture preservation and additive v1 migration."""

from __future__ import annotations

import copy
import json
import sqlite3

import blueprint_report as adapter
from study_sync import digest, iso, timestamp, validate_snapshot


def from_v1(snapshot):
    validate_snapshot(snapshot)
    result = copy.deepcopy(snapshot)
    result["schema_version"] = 2
    for row in result["attempts"]:
        row.update(
            source="blueprint_mcat",
            outcome="correct"
            if row["correct"] is True
            else "incorrect"
            if row["correct"] is False
            else "unknown",
            prior_exposure="unknown",
            help="unknown",
            condition_basis="unavailable",
            identity_basis="legacy_provider_id",
            complete_scope=False,
            occurrence_basis="source_start_time",
            revision_basis="source_last_seen_unverified",
            timestamp_precision="source",
            legacy_aliases=[row["id"]],
        )
    for row in result["assessments"]:
        row.update(
            provider="blueprint",
            scale="provider_reported",
            prior_exposure="unknown",
            help="unknown",
        )
    return result


def from_bundle(bundle):
    # Reproject before saving: arbitrary caller fields, cookies and page text cannot enter the archive.
    clean = adapter.make_bundle(
        bundle["score"],
        bundle["identity"],
        bundle["learner_id"],
        bundle["activity_id"],
        bundle["captured_at"],
        bundle["response_captured_at"],
    )
    snapshot = from_v1(adapter.normalize(clean))
    _, score = adapter.select_activity(clean["score"], clean["activity_id"])
    ie, identity = adapter.select_activity(clean["identity"], clean["activity_id"])
    rows = {r["question_id"]: r for r in snapshot["attempts"]}
    ids = {s["section"]["shortTitle"]: s for s in identity["sections"]}
    for section in score["sections"]:
        identity_section = ids[section["section"]["shortTitle"]]
        for group, identity_group in zip(
            section["questionGroups"], identity_section["questionGroups"]
        ):
            for question, identity_question in zip(
                group["questions"], identity_group["questions"]
            ):
                row = rows[identity_question["question"]["id"]]
                row.update(
                    group_id=identity_group["questionGroup"]["id"],
                    discrete=group["questionGroup"]["discrete"],
                    section_id=identity_section["section"]["id"],
                    provider_session_id=clean["activity_id"],
                    exam_id=ie["id"],
                    identity_basis="provider_activity_and_question",
                    identity_caveat="Retakes reusing the activity ID cannot be distinguished without provider evidence.",
                    selected_answer=question["answer"],
                    previous_answer=question["previousAnswer"],
                    tags=question["question"]["metadata"],
                    raw_aggregate=question["aggregate"],
                    complete_scope=True,
                    scope_id=row["assessment_id"],
                    scope_expected=score["aggregate"]["total"],
                )
                if row["correct"] is None:
                    row["outcome"] = (
                        "omitted"  # Confirmed by complete, reconciled aggregate.
                    )
    snapshot["assessments"][0].update(
        exam_id=ie["id"],
        percentile=score.get("percentile"),
        raw_aggregate=score["aggregate"],
        sections=score["sections"],
    )
    return snapshot, clean


def reconcile_revision(kind, current, incoming):
    """Return the disposition and the record to publish for one source revision."""
    if current is None:
        return "inserted", incoming
    if kind == "attempts" and current["occurred_at"] != incoming["occurred_at"]:
        return "conflict", None

    # Both revision clocks are stored as canonical UTC strings with microseconds.
    if incoming["updated_at"] < current["updated_at"]:
        return "older", None
    if incoming["updated_at"] > current["updated_at"]:
        return "updated", incoming

    fields = dict(incoming)
    shared = current.keys() & fields.keys()
    if kind == "attempts" and incoming["complete_scope"] != current["complete_scope"]:
        # Only a reconciled report can establish scope, identity and omissions.
        richer = incoming if incoming["complete_scope"] else current
        derived = {"complete_scope", "identity_basis", "outcome"}
        shared -= derived
        for key in derived:
            fields[key] = richer[key]
    if any(current[key] != fields[key] for key in shared):
        return "conflict", None
    merged = {**current, **fields}
    if merged == current:
        return "unchanged", None
    return "updated", merged


def ingest(store, data, request_id=None):
    if data.get("format") == "blueprint_report_capture":
        snapshot, capture = from_bundle(data)
    else:
        snapshot, capture = from_v1(data), None
    request_id = request_id or digest(data)

    def save():
        owner = store.get("source_owner", "blueprint")
        if owner and owner["source_learner"] != snapshot["learner_id"]:
            raise ValueError("Source learner mismatch")
        if not owner:
            store.put(
                "source_owner", "blueprint", {"source_learner": snapshot["learner_id"]}
            )
        capture_id = digest(snapshot)
        if store.get("capture", capture_id):
            return {"status": "already_imported", "capture_id": capture_id}
        conflicts = []
        revisions = []
        for kind in ("attempts", "assessments", "activities"):
            for raw in snapshot[kind]:
                row = {
                    **raw,
                    "occurred_at": iso(timestamp(raw["occurred_at"])),
                    "updated_at": iso(timestamp(raw["updated_at"])),
                }
                current = store.get(kind, row["id"])
                disposition, merged = reconcile_revision(kind, current, row)
                revisions.append((kind, row, disposition, merged))
                if disposition == "conflict":
                    conflicts.append(row["id"])
        status = "quarantined" if conflicts else "accepted"
        store.put(
            "capture",
            capture_id,
            {
                "source": "blueprint_mcat",
                "captured_at": snapshot["captured_at"],
                "coverage": snapshot["coverage"],
                "capture_metadata": snapshot.get("capture_metadata"),
                "payload": capture or data,
                "status": status,
                "conflicts": sorted(set(conflicts)),
            },
        )
        if conflicts:
            return {
                "status": status,
                "capture_id": capture_id,
                "conflicts": sorted(set(conflicts)),
            }
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "older": 0}
        for kind, row, disposition, merged in revisions:
            revision_id = digest([kind, row])
            if not store.get("source_revision", revision_id):
                store.put(
                    "source_revision",
                    revision_id,
                    {"kind": kind, "record": row, "capture_id": capture_id},
                )
            if merged is not None:
                store.put(kind, row["id"], {**merged, "capture_id": capture_id})
            counts[disposition] += 1
        return {"status": status, "capture_id": capture_id, **counts}

    return store.command("ingest", request_id, data, save)


def migrate_v1(store, path):
    """Read the old DB without modifying it. Mark migration receipt honestly."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        owner = db.execute("SELECT value FROM meta WHERE key='learner_id'").fetchone()
        if not owner:
            return {"status": "empty"}
        revisions = db.execute(
            "SELECT kind,payload,observed_at FROM revisions ORDER BY observed_at"
        ).fetchall()
        imported = 0
        for kind, payload, observed in revisions:
            row = json.loads(payload)
            data = {
                "schema_version": 1,
                "source": "blueprint_mcat",
                "learner_id": owner[0],
                "captured_at": row["updated_at"],
                "coverage": {
                    "attempts": "partial",
                    "assessments": "partial",
                    "activities": "partial",
                },
                "attempts": [],
                "assessments": [],
                "activities": [],
            }
            data[kind] = [row]
            result = ingest(store, data, "migration:" + digest([kind, row]))
            with store.transaction():
                if not store.get("migration_provenance", digest([kind, row])):
                    store.put(
                        "migration_provenance",
                        digest([kind, row]),
                        {
                            "legacy_observed_at": observed,
                            "capture_id": result["capture_id"],
                            "limit": "Original capture time unavailable for this revision; updated_at is a lower bound.",
                        },
                    )
            imported += 1
        for (payload,) in db.execute("SELECT payload FROM observations"):
            row = json.loads(payload)
            with store.transaction():
                if not store.get("observation", row["id"]):
                    store.put("observation", row["id"], row)
        return {"status": "migrated", "source_revisions": imported}
    finally:
        db.close()
