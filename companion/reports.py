"""Timezone-aware calendar contracts, immutable inputs, and original narratives."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from companion import POLICY_VERSION
from companion.metrics import readiness, repair_metrics, strata, topics
from study_sync import digest, iso, timestamp


def period(kind, as_of, timezone):
    zone = ZoneInfo(timezone)
    local = timestamp(as_of).astimezone(zone)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        start = end - timedelta(days=1)
        comparison_start = start - timedelta(days=1)
    elif kind == "weekly":
        end -= timedelta(days=end.weekday())
        start, comparison_start = end - timedelta(days=7), end - timedelta(days=14)
    elif kind == "monthly":
        end = end.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        comparison_start = (start - timedelta(days=1)).replace(day=1)
    else:
        raise ValueError("Report kind must be daily, weekly, or monthly")
    return {
        "kind": kind,
        "timezone": timezone,
        "start": iso(start),
        "end": iso(end),
        "local_start": start.isoformat(),
        "local_end": end.isoformat(),
        "comparison_start": iso(comparison_start),
        "comparison_end": iso(start),
    }


def between(row, start, end):
    return bool(row.get("occurred_at")) and timestamp(start) <= timestamp(
        row["occurred_at"]
    ) < timestamp(end)


def freshness(store, cutoff):
    result = {}
    prefs = store.get("preferences", "current", cutoff) or {}
    for source, kind, field in (
        ("blueprint", "capture", "captured_at"),
        ("context_upload", "context_document", "received_at"),
        ("lessons", "lesson_event", "occurred_at"),
    ):
        rows = store.list(kind, cutoff)
        if kind == "capture":
            rows = [r for r in rows if r["status"] == "accepted"]
        times = [timestamp(r[field]) for r in rows if r.get(field)]
        latest = max(times) if times else None
        result[source] = {
            "last_evidence_at": iso(latest) if latest else None,
            "age_hours": max(0, (timestamp(cutoff) - latest).total_seconds() / 3600)
            if latest
            else None,
            "availability": "available" if latest else "unavailable",
        }
        if source == "blueprint":
            result[source]["stale"] = latest is None or timestamp(
                cutoff
            ) - latest > timedelta(hours=prefs.get("freshness_hours", 48))
            result[source]["scope"] = (
                "Captured reports only; account history remains partial."
            )
            result[source]["collections"] = {
                k: max(
                    (
                        r["captured_at"]
                        for r in rows
                        if r["coverage"].get(k) != "unavailable"
                    ),
                    default=None,
                )
                for k in ("attempts", "assessments", "activities")
            }
    return result


def context(store, kind, as_of=None, timezone=None, period_as_of=None):
    cutoff = as_of or store.now()
    if timestamp(cutoff) > store.clock():
        raise ValueError("Data cutoff cannot be in the future")
    prefs = store.get("preferences", "current", cutoff) or {}
    timezone = timezone or prefs.get("timezone")
    if not timezone:
        raise ValueError(
            "Set the learner's IANA timezone before creating calendar reports"
        )
    window = period(kind, period_as_of or cutoff, timezone)
    if timestamp(window["end"]) > timestamp(cutoff):
        raise ValueError("The reporting period must end before the evidence cutoff")
    all_rows = store.list("attempts", cutoff) + store.list("lesson_attempt", cutoff)
    quarantined = {
        r["id"] for r in store.list("item", cutoff) if r.get("status") == "quarantined"
    }
    all_rows = [
        {**r, "outcome": "unscorable"} if r.get("item_ref") in quarantined else r
        for r in all_rows
    ]
    rows = [r for r in all_rows if between(r, window["start"], window["end"])]
    previous = [
        r
        for r in all_rows
        if between(r, window["comparison_start"], window["comparison_end"])
    ]
    forms = store.list("assessment", cutoff) + store.list("assessments", cutoff)
    forms = [f for f in forms if timestamp(f["occurred_at"]) <= timestamp(cutoff)]
    events = [
        e
        for e in store.list("lesson_event", cutoff)
        if between(e, window["start"], window["end"])
    ]
    offers = [
        o
        for o in store.list("offer", cutoff)
        if window["start"] <= iso(timestamp(o["created_at"])) < window["end"]
    ]
    repairs = store.list("repair", cutoff)
    results = [
        r
        for r in store.list("probe_result", cutoff)
        if r.get("item_ref") not in quarantined
    ]
    from companion.policy import candidates

    actions = candidates(store, cutoff)[:3]
    scopes = {}
    for row in all_rows:
        if row.get("scope_id") and row.get("scope_expected") is not None:
            scope = scopes.setdefault(
                row["scope_id"],
                {
                    "scope_id": row["scope_id"],
                    "captured_ids": set(),
                    "expected": row["scope_expected"],
                },
            )
            scope["captured_ids"].add(row["id"])
    coverage = [
        {
            "scope_id": s["scope_id"],
            "captured": len(s["captured_ids"]),
            "expected": s["expected"],
            "complete": len(s["captured_ids"]) == s["expected"],
        }
        for s in scopes.values()
    ]
    result = {
        "schema_version": 2,
        "period": window,
        "data_cutoff": cutoff,
        "input_revision": store.evidence_revision(cutoff),
        "source_freshness": freshness(store, cutoff),
        "coverage_scopes": coverage,
        "current": strata(rows),
        "comparison": strata(previous),
        "topics": topics(rows),
        "comparison_limit": "Compare matching conditions and topic mix; counts alone do not establish a trend.",
        "rolling_context": strata(
            [
                r
                for r in all_rows
                if timestamp(cutoff) - timedelta(days=28 if kind == "weekly" else 90)
                <= timestamp(r["occurred_at"])
                <= timestamp(cutoff)
            ]
        ),
        "lesson_activity": {
            "offered": len(offers),
            **{
                k: sum(e["type"] == k for e in events)
                for k in (
                    "started",
                    "paused",
                    "partial",
                    "completed",
                    "skipped",
                    "expired",
                    "cancelled",
                )
            },
        },
        "repair_outcomes": {
            purpose: repair_metrics(
                [r for r in repairs if r["purpose"] == purpose],
                [r for r in results if r["purpose"] == purpose],
                cutoff,
            )
            for purpose in ("delayed_recall", "delayed_transfer")
        },
        "readiness": readiness(forms, prefs, cutoff),
        "assessment_evidence": [
            {
                k: f.get(k)
                for k in (
                    "id",
                    "provider",
                    "name",
                    "form_id",
                    "occurred_at",
                    "scores",
                    "mode",
                    "prior_exposure",
                    "help",
                    "conditions",
                    "conditions_comparable",
                    "scale",
                )
            }
            for f in sorted(forms, key=lambda f: f["occurred_at"], reverse=True)[:30]
        ],
        "assessment_evidence_total": len(forms),
        "challenge_evidence": strata([r for r in rows if r.get("challenge_intent")]),
        "evidence_ids": [r["id"] for r in rows],
        "recommendations": actions,
        "unknowns": [
            "Mini-lessons do not establish endurance or scaled-score readiness.",
            "First observed is not first-ever exposure.",
            "No captured activity does not mean no studying.",
        ],
        "policy_version": POLICY_VERSION,
        "prompt_version": "reports-v2",
    }
    metrics = {}
    for ix, group in enumerate(result["current"]):
        for name in (
            "answered_accuracy",
            "scored_accuracy",
            "fresh_independent_accuracy",
        ):
            metrics[f"current.{ix}.{name}"] = group[name]
    result["metrics"] = metrics
    result["snapshot_id"] = digest(result)
    if not store.get("metric_snapshot", result["snapshot_id"]):
        store.put("metric_snapshot", result["snapshot_id"], result)
    return result


def rate_text(r):
    return (
        "unavailable"
        if r["value"] is None
        else f"{r['numerator']}/{r['denominator']} ({r['value']:.1%})"
    )


def render(ctx):
    p = ctx["period"]
    lines = [
        f"{p['kind'].capitalize()} progress report: {p['local_start'][:10]} to {p['local_end'][:10]} (end exclusive)",
        f"Timezone: {p['timezone']}. Evidence cutoff: {ctx['data_cutoff']}.",
        "",
        "Data quality",
    ]
    for name, value in ctx["source_freshness"].items():
        lines.append(
            f"{name}: {value['last_evidence_at'] or 'unavailable'}"
            + (" — stale" if value.get("stale") else "")
        )
    lines.extend(
        [
            "Coverage is limited to captured reports and saved companion events.",
            "",
            "Study process",
            "",
            "| Source / section / help / exposure | Correct / answered | Scored accuracy |",
            "|---|---|---|",
        ]
    )
    for group in ctx["current"]:
        c = group["conditions"]
        label = " / ".join(
            str(c[k]).replace("|", "/")
            for k in ("source", "section", "help", "prior_exposure")
        )
        lines.append(
            f"| {label} | {rate_text(group['answered_accuracy'])} | {rate_text(group['scored_accuracy'])} |"
        )
    if not ctx["current"]:
        lines.append(
            "No new captured attempts occurred in this period. Existing priorities remain available; no movement is inferred."
        )
    activity = ctx["lesson_activity"]
    lines.append(
        f"Optional offers: {activity['offered']}; started: {activity['started']}; completed: {activity['completed']}; paused: {activity['paused']}. Unused offers carry no failure or obligation."
    )
    lines.extend(["", "Demonstrated learning"])
    for purpose, metric in ctx["repair_outcomes"].items():
        lines.append(
            f"{purpose}: verified/matured {rate_text(metric['verified_matured'])}; tested/matured {rate_text(metric['tested_matured'])}; passed/tested {rate_text(metric['passed_tested'])}."
        )
    lines.append(
        "Untested repairs remain unverified. Assisted and immediate successes do not demonstrate delayed independent learning."
    )
    lines.extend(
        [
            "",
            "Exam readiness",
            ctx["readiness"]["label"] + ": " + " ".join(ctx["readiness"]["reasons"]),
            "Challenge drills are separate from representative official forms; no score conversion or prediction is made.",
            "",
            "Next actions",
        ]
    )
    for action in ctx["recommendations"]:
        lines.append(
            f"- {action['action']} Why: {action['reason']} Verify with: {action['verification_criterion']}"
        )
    if not ctx["recommendations"]:
        lines.append(
            "No supported priority yet. Upload context or capture a completed report to identify a narrow diagnostic objective."
        )
    if p["kind"] != "daily":
        lines.extend(
            [
                "",
                "Review of the plan",
                ctx["comparison_limit"],
                "Retain the current approach pending independent outcomes. Optional lesson time is not committed study time.",
                "Goals, exam date and available time remain unknown unless explicitly configured. Broad readiness and causal improvement are not established by lesson completion.",
            ]
        )
    return "\n".join(lines)


def save(
    store, snapshot_id, narrative=None, model_id="deterministic", correction_reason=None
):
    ctx = store.get("metric_snapshot", snapshot_id)
    if not ctx:
        raise ValueError("Unknown metric snapshot")
    canonical = render(ctx)
    if narrative is not None and narrative != canonical:
        # Arbitrary LLM prose cannot be certified numerically by a schema or regex.
        raise ValueError(
            "Use the deterministic narrative for validated reports; free-form commentary belongs in labeled observations"
        )
    identifier = digest([store.learner, ctx["period"]])
    old = store.get("report", identifier)
    if old and (
        old["snapshot_id"] == snapshot_id
        or old["input_revision"] == ctx["input_revision"]
        and not correction_reason
    ):
        return old
    if old and not correction_reason:
        raise ValueError(
            "A superseding report needs a correction_reason; the old narrative is retained"
        )
    return store.put(
        "report",
        identifier,
        {
            "snapshot_id": snapshot_id,
            "period": ctx["period"],
            "data_cutoff": ctx["data_cutoff"],
            "input_revision": ctx["input_revision"],
            "metric_ids": list(ctx["metrics"]),
            "evidence_ids": ctx["evidence_ids"],
            "narrative": canonical,
            "model_id": model_id,
            "prompt_version": ctx["prompt_version"],
            "policy_version": POLICY_VERSION,
            "publication_state": "persisted",
            "supersedes_version": old["version"] if old else None,
            "correction_reason": correction_reason,
        },
    )
