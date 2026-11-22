"""Idempotent event outbox. Never synchronize whole SQLite files or overwrite hosted history."""

from datetime import timedelta
from urllib.parse import urlsplit

from companion.ingest import from_bundle, ingest
from study_sync import digest, iso, timestamp

PUBLIC_KINDS = {
    "attempts",
    "assessments",
    "activities",
    "lesson_event",
    "lesson_attempt",
    "report",
    "preferences",
    "probe_result",
    "repair",
    "evidence_state",
    "context_document",
    "context_entry",
    "delivery",
}


def enqueue(store, bundle):
    _, clean = from_bundle(bundle)
    identifier = digest(clean)
    with store.transaction():
        if not store.get("outbox", identifier):
            store.put(
                "outbox",
                identifier,
                {
                    "payload": clean,
                    "state": "pending",
                    "attempts": 0,
                    "next_retry": None,
                },
            )
    return identifier


def receive(store, events):
    if not isinstance(events, list) or not 1 <= len(events) <= 20:
        raise ValueError("Sync batches must contain 1..20 events")
    results = []
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {"event_id", "payload"}
            or event["event_id"] != digest(event["payload"])
        ):
            raise ValueError("Invalid sync event identity")
        result = ingest(store, event["payload"], "sync:" + event["event_id"])
        results.append({"event_id": event["event_id"], **result})
    return {"acknowledgements": results}


def changes(store, cursor=0, limit=100):
    if (
        type(cursor) is not int
        or cursor < 0
        or type(limit) is not int
        or not 1 <= limit <= 200
    ):
        raise ValueError("Invalid sync cursor/limit")
    import json

    rows = store.sql(
        "SELECT seq,kind,id,version,body FROM entity_versions WHERE seq>? ORDER BY seq LIMIT ?",
        (cursor, limit),
    ).fetchall()
    return {
        "cursor": rows[-1][0] if rows else cursor,
        "changes": [
            {
                "sequence": r[0],
                "kind": r[1],
                "id": r[2],
                "version": r[3],
                "data": json.loads(r[4]),
            }
            for r in rows
            if r[1] in PUBLIC_KINDS
        ],
        "has_more": bool(rows) and rows[-1][0] < store.revision(),
    }


def synchronize(store, url, token, client=None):
    if (
        urlsplit(url).scheme != "https"
        or urlsplit(url).username
        or urlsplit(url).password
    ):
        raise ValueError("Hosted sync requires HTTPS without credentials in the URL")
    if not token or len(token) < 32:
        raise ValueError(
            "Configure a separate collector token of at least 32 characters"
        )
    import httpx

    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    headers = {"Authorization": "Bearer " + token}
    sent = 0
    try:
        pending = [
            r
            for r in store.list("outbox")
            if r["state"] == "pending"
            and r["attempts"] < 3
            and (not r["next_retry"] or timestamp(r["next_retry"]) <= store.clock())
        ][:20]
        if pending:
            payload = [{"event_id": r["id"], "payload": r["payload"]} for r in pending]
            # Persist an attempt before network I/O; crashes leave a visible pending retry.
            with store.transaction():
                for row in pending:
                    store.put(
                        "outbox",
                        row["id"],
                        {
                            **row,
                            "attempts": row["attempts"] + 1,
                            "next_retry": iso(
                                store.clock()
                                + timedelta(minutes=2 ** (row["attempts"] + 1))
                            ),
                        },
                    )
            response = client.post(
                url.rstrip("/") + "/sync/batches",
                json={"events": payload},
                headers=headers,
            )
            response.raise_for_status()
            ack = response.json()["acknowledgements"]
            if {r["event_id"] for r in ack} != {r["id"] for r in pending}:
                raise ValueError(
                    "Server acknowledgement did not match the uploaded batch"
                )
            with store.transaction():
                for result in ack:
                    row = store.get("outbox", result["event_id"])
                    if result["status"] not in {
                        "accepted",
                        "already_imported",
                        "quarantined",
                    }:
                        raise ValueError("Unknown ingestion acknowledgement")
                    store.put(
                        "outbox",
                        row["id"],
                        {
                            **row,
                            "state": "quarantined"
                            if result["status"] == "quarantined"
                            else "acknowledged",
                            "acknowledgement": result,
                        },
                    )
                    sent += 1
        cursor = (store.get("sync_cursor", "hosted") or {}).get("cursor", 0)
        # Bounded page count; next run resumes. Hosted events have their own namespace.
        for _ in range(10):
            response = client.get(
                url.rstrip("/") + "/sync/changes",
                params={"cursor": cursor},
                headers=headers,
            )
            response.raise_for_status()
            page = response.json()
            if type(page.get("cursor")) is not int or page["cursor"] < cursor:
                raise ValueError("Invalid server cursor")
            with store.transaction():
                for change in page["changes"]:
                    identifier = str(change["sequence"])
                    if not store.get("hosted_change", identifier):
                        store.put("hosted_change", identifier, change)
                store.put(
                    "sync_cursor",
                    "hosted",
                    {"cursor": page["cursor"], "synced_at": store.now()},
                )
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        return {"status": "synchronized", "acknowledged": sent, "cursor": cursor}
    finally:
        if owns_client:
            client.close()
