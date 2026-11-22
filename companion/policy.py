"""Transparent candidate selection; no penalty for missed optional offers."""

from datetime import timedelta

from study_sync import timestamp


def target_key(target):
    return tuple(target[k] for k in ("section", "concept", "skill"))


def candidates(store, cutoff=None):
    cutoff = cutoff or store.now()
    output = []
    quarantined = {
        i["id"] for i in store.list("item", cutoff) if i["status"] == "quarantined"
    }
    results = [
        r
        for r in store.list("probe_result", cutoff)
        if r.get("item_ref") not in quarantined
    ]
    demonstrated = {}
    for result in sorted(results, key=lambda r: r["occurred_at"]):
        if result["eligible"]:
            repair = store.get("repair", result["repair_id"], cutoff)
            if repair and repair["purpose"] == "delayed_transfer":
                demonstrated[target_key(repair["target"])] = result
    for repair in store.list("repair", cutoff):
        if timestamp(repair["due_at"]) > timestamp(cutoff):
            continue
        eligible = sorted(
            [r for r in results if r["repair_id"] == repair["id"] and r["eligible"]],
            key=lambda r: r["occurred_at"],
        )
        successful = bool(eligible) and eligible[-1]["passed"]
        if successful:
            continue
        output.append(
            {
                "priority": 1,
                "target": repair["target"],
                "repair_id": repair["id"],
                "purpose": repair["purpose"],
                "reason": "A repaired objective still needs independent delayed evidence.",
                "action": "Try a new short independent check when convenient.",
                "mechanism": "unconfirmed",
                "evidence_ids": repair["origin_evidence_ids"],
                "estimated_minutes": 5,
                "verification_criterion": repair["pass_criterion"],
                "optional_or_committed": "optional",
            }
        )
    for note in store.list("context_entry", cutoff):
        if note.get("reason") not in {
            "did_not_know",
            "forgot",
            "confused",
        } or not note.get("target"):
            continue
        output.append(
            {
                "priority": 2,
                "target": note["target"],
                "purpose": "diagnostic",
                "repair_id": None,
                "reason": "An uploaded learner note reports confusion; the mechanism remains unconfirmed.",
                "action": "Try a brief diagnostic on the reported concept.",
                "mechanism": "unconfirmed",
                "evidence_ids": [note["id"]],
                "estimated_minutes": 7,
                "verification_criterion": "A new independent delayed application with the rule explained.",
                "optional_or_committed": "optional",
            }
        )
    rows = store.list("attempts", cutoff) + store.list("lesson_attempt", cutoff)
    recent = timestamp(cutoff) - timedelta(days=30)
    for row in sorted(rows, key=lambda r: r["occurred_at"], reverse=True):
        if not recent <= timestamp(row["occurred_at"]) <= timestamp(cutoff):
            continue
        if row.get("outcome") != "incorrect" and not row.get("bookmarked"):
            continue
        target = {
            "section": row["section"],
            "concept": row.get("topic", "unknown"),
            "skill": row.get("skill") or "unknown",
        }
        later = demonstrated.get(target_key(target))
        if (
            later
            and later["passed"]
            and timestamp(later["occurred_at"]) > timestamp(row["occurred_at"])
        ):
            continue
        output.append(
            {
                "priority": 3,
                "target": {
                    "section": row["section"],
                    "concept": row.get("topic", "unknown"),
                    "skill": row.get("skill") or "unknown",
                },
                "purpose": "diagnostic",
                "repair_id": None,
                "reason": "A captured incorrect or flagged response warrants a diagnostic; a flag is not confidence.",
                "action": "Attempt a narrow concept/application diagnostic with brief reasoning.",
                "mechanism": "unconfirmed",
                "evidence_ids": [row["id"]],
                "estimated_minutes": 7,
                "verification_criterion": "Independent success on a new application after a delay.",
                "optional_or_committed": "optional",
            }
        )
    unique = {}
    for candidate in sorted(output, key=lambda c: c["priority"]):
        key = target_key(candidate["target"])
        if key not in unique:
            unique[key] = candidate
    return list(unique.values())[:30]


def choose_items(store, candidate, cutoff):
    shown = {e["family"] for e in store.list("exposure", cutoff)}
    available = [
        i
        for i in store.list("item", cutoff)
        if i["status"] == "accepted"
        and i["target"] == candidate["target"]
        and i["family"] not in shown
    ]
    primary = [i for i in available if i["purpose"] == candidate["purpose"]]
    if not primary:
        return []
    first = min(primary, key=lambda i: i["id"])
    if candidate["purpose"].startswith("delayed_"):
        repair = store.get("repair", candidate["repair_id"], cutoff)
        if first["family"] in repair["excluded_families"]:
            primary = [
                i for i in primary if i["family"] not in repair["excluded_families"]
            ]
            if not primary:
                return []
            first = min(primary, key=lambda i: i["id"])
        return [first["id"]]
    second = next(
        (
            i
            for i in sorted(available, key=lambda i: i["id"])
            if i["purpose"] == "immediate_application"
            and i["family"] != first["family"]
        ),
        None,
    )
    return [first["id"], second["id"]] if second else []
