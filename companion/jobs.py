"""Backend due records and leases, without a second notification scheduler."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from study_sync import digest, iso, timestamp

CAPABILITIES = {
    "interactive_read",
    "interactive_write",
    "scheduled_read",
    "scheduled_write",
    "scheduled_reply_write",
    "notifications",
    "task_capacity",
}


def record_capability(store, name, status, evidence):
    if name not in CAPABILITIES or status not in {"verified", "failed", "unverified"}:
        raise ValueError("Invalid capability record")
    if not evidence.strip():
        raise ValueError("Describe the observed test and result")
    return store.put(
        "capability",
        name,
        {"status": status, "evidence": evidence[:3000], "tested_at": store.now()},
    )


def ready(store, mode=None):
    records = {r["id"]: r["status"] for r in store.list("capability")}
    mode = mode or (store.get("preferences", "current") or {}).get(
        "scheduled_delivery_mode", "read_write"
    )
    required = (
        CAPABILITIES - {"scheduled_write"}
        if mode == "backend_prepared_reads"
        else CAPABILITIES
    )
    return all(records.get(k) == "verified" for k in required)


def prepare(store):
    prefs = store.get("preferences", "current") or {}
    if not prefs.get("schedules_enabled"):
        return {"availability": "schedules_disabled", "jobs": []}
    if not ready(store):
        return {"availability": "capability_gate_unverified", "jobs": []}
    if not prefs.get("timezone"):
        raise ValueError("Learner timezone required")
    zone = ZoneInfo(prefs["timezone"])
    local = store.clock().astimezone(zone)
    slots = [("daily", "08:00")]
    if local.weekday() == 0:
        slots.append(("weekly", "08:15"))
    if local.day == 1:
        slots.append(("monthly", "08:30"))
    slots += [("lesson", t) for t in prefs.get("lesson_slots", [])]
    due = []
    for kind, slot in slots:
        wall = datetime.combine(
            local.date(), datetime.strptime(slot, "%H:%M").time(), zone
        )
        utc = timestamp(wall.isoformat())
        # Policy: skip nonexistent wall time; run repeated time once (fold=0).
        if utc.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
            continue
        lag = store.clock() - utc
        if lag < timedelta(0) or lag > timedelta(hours=2 if kind == "lesson" else 12):
            continue
        # Exclude timezone from logical slot identity so a timezone change cannot replay a delivered date/slot.
        identifier = digest([store.learner, local.date().isoformat(), kind, slot])
        job = store.get("job", identifier)
        if not job:
            job = store.put(
                "job",
                identifier,
                {
                    "kind": kind,
                    "slot": slot,
                    "local_date": local.date().isoformat(),
                    "timezone": prefs["timezone"],
                    "due_at": iso(utc),
                    "state": "due",
                    "attempts": 0,
                    "lease_until": None,
                    "next_retry": None,
                    "output": None,
                },
            )
        due.append({k: job[k] for k in ("id", "kind", "slot", "state", "due_at")})
    return {"availability": "ready", "jobs": due}


def claim(store, identifier, worker):
    job = store.get("job", identifier)
    if not job or job["state"] in {"render_ready", "failed"}:
        return None
    if job.get("lease_until") and timestamp(job["lease_until"]) > store.clock():
        return None
    if job.get("next_retry") and timestamp(job["next_retry"]) > store.clock():
        return None
    if job["attempts"] >= 3:
        store.put("job", identifier, {**job, "state": "failed"})
        return None
    return store.put(
        "job",
        identifier,
        {
            **job,
            "state": "claimed",
            "worker": worker,
            "lease_until": iso(store.clock() + timedelta(minutes=5)),
            "attempts": job["attempts"] + 1,
        },
    )


def finish(store, identifier, worker, output=None, error=None):
    job = store.get("job", identifier)
    if (
        not job
        or job.get("worker") != worker
        or not job.get("lease_until")
        or timestamp(job["lease_until"]) <= store.clock()
    ):
        raise ValueError("Job lease is no longer held")
    state = (
        "failed"
        if error and job["attempts"] >= 3
        else "due"
        if error
        else "render_ready"
    )
    return store.put(
        "job",
        identifier,
        {
            **job,
            "state": state,
            "lease_until": None,
            "output": output,
            "error": error,
            "next_retry": iso(store.clock() + timedelta(minutes=2 ** job["attempts"]))
            if error
            else None,
        },
    )
