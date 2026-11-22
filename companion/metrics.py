"""Deterministic descriptive metrics. No mastery or MCAT score conversion."""

from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from study_sync import timestamp


def rate(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def performance(rows):
    outcomes = Counter(r.get("outcome", "unknown") for r in rows)
    c, i, o = (outcomes[k] for k in ("correct", "incorrect", "omitted"))
    scored = [
        r
        for r in rows
        if r.get("complete_scope")
        and r.get("outcome") in {"correct", "incorrect", "omitted"}
    ]
    fresh = [
        r
        for r in scored
        if r.get("help") == "independent" and r.get("prior_exposure") == "confirmed_new"
    ]
    changes = Counter(r.get("answer_change") for r in rows)
    probabilities = [
        (r["confidence"]["value"], r["outcome"] == "correct")
        for r in rows
        if r.get("confidence")
        and r["confidence"].get("scale") == "probability"
        and r["confidence"].get("before_feedback") is True
        and r.get("outcome") in {"correct", "incorrect"}
    ]
    return {
        "attempts": len(rows),
        "unique_items": len({r["question_id"] for r in rows}),
        "known_passages": len(
            {
                r["group_id"]
                for r in rows
                if r.get("group_id") and r.get("discrete") is False
            }
        ),
        "unknown_group_count": sum(not r.get("group_id") for r in rows),
        "known_sessions": len(
            {
                r.get("assessment_id") or r.get("session_id")
                for r in rows
                if r.get("assessment_id") or r.get("session_id")
            }
        ),
        "correct": c,
        "incorrect": i,
        "confirmed_omissions": o,
        "unknown_outcomes": outcomes["unknown"],
        "unscorable": outcomes["unscorable"],
        "answered_accuracy": rate(c, c + i),
        "scored_accuracy": rate(
            sum(r["outcome"] == "correct" for r in scored), len(scored)
        ),
        "scored_excluded": len(rows) - len(scored),
        "fresh_independent_accuracy": rate(
            sum(r["outcome"] == "correct" for r in fresh), len(fresh)
        ),
        "fresh_independent_excluded": len(rows) - len(fresh),
        "net_answer_change": changes["wrong_to_right"] - changes["right_to_wrong"],
        "answer_change_known": sum(r.get("answer_change") is not None for r in rows),
        "confidence_brier": {
            "value": sum((p - y) ** 2 for p, y in probabilities) / len(probabilities)
            if probabilities
            else None,
            "count": len(probabilities),
            "limit": "Pre-feedback probabilities only; not a pure calibration measure.",
        },
    }


def strata(rows):
    groups = defaultdict(list)
    for row in rows:
        key = tuple(
            row.get(k) or "unknown"
            for k in ("source", "section", "mode", "help", "prior_exposure")
        )
        groups[key].append(row)
    return [
        {
            "conditions": dict(
                zip(("source", "section", "mode", "help", "prior_exposure"), key)
            ),
            **performance(value),
        }
        for key, value in sorted(groups.items())
    ]


def topics(rows):
    groups = defaultdict(dict)
    for row in rows:
        for topic in row.get("topics") or [row.get("topic", "unknown")]:
            groups[(row["section"], topic)][row["id"]] = row
    return [
        {
            "section": k[0],
            "topic": k[1],
            "overlapping_denominators": True,
            "strata": strata(list(v.values())),
        }
        for k, v in sorted(groups.items())
    ]


def repair_metrics(repairs, results, cutoff):
    matured = [r for r in repairs if timestamp(r["window_end"]) <= timestamp(cutoff)]
    tested, passed = 0, 0
    for repair in matured:
        eligible = [
            r
            for r in results
            if r["repair_id"] == repair["id"] and r["eligible"] and r["in_window"]
        ]
        # Prespecified first eligible test; do not cherry-pick a later pass.
        eligible.sort(key=lambda r: r["occurred_at"])
        if eligible:
            tested += 1
            passed += eligible[0]["passed"] is True
    return {
        "verified_matured": rate(passed, len(matured)),
        "tested_matured": rate(tested, len(matured)),
        "passed_tested": rate(passed, tested),
        "untested": len(matured) - tested,
        "late_results": sum(not r["in_window"] for r in results),
    }


def readiness(forms, preferences, cutoff):
    goals = preferences.get("goals") or {}
    minimum = preferences.get("readiness_min_forms", 3)
    horizon = preferences.get("readiness_horizon_days", 90)
    recent = timestamp(cutoff) - timedelta(days=horizon)
    eligible = [
        f
        for f in forms
        if f.get("provider") == "aamc"
        and f.get("prior_exposure") == "confirmed_new"
        and f.get("help") == "independent"
        and f.get("conditions_comparable") is True
        and f.get("complete") is True
        and all(
            type(f["scores"].get(k)) is int for k in ("total", "CP", "CARS", "BB", "PS")
        )
        and f["scores"]["total"]
        == sum(f["scores"][k] for k in ("CP", "CARS", "BB", "PS"))
        and recent <= timestamp(f["occurred_at"]) <= timestamp(cutoff)
    ]
    eligible.sort(key=lambda f: f["occurred_at"], reverse=True)
    # A repeated form is never fresh readiness evidence, even if mislabeled on upload.
    counts = Counter(f.get("form_id") for f in forms if f.get("provider") == "aamc")
    eligible = [f for f in eligible if f.get("form_id") and counts[f["form_id"]] == 1]
    scores = [
        f["scores"]["total"] for f in eligible if f["scores"].get("total") is not None
    ]
    complete_goals = set(goals) == {"total", "CP", "CARS", "BB", "PS"}
    reasons = []
    if not complete_goals:
        reasons.append("Total and section goals are not all configured.")
    if len(eligible) < minimum:
        reasons.append("Too few recent comparable fresh official forms.")
    if reasons:
        label = "insufficient_evidence"
    else:
        passing = all(
            all((f["scores"].get(k) or 0) >= goal for k, goal in goals.items())
            for f in eligible[:minimum]
        )
        label = "consistent_recent_demonstration" if passing else "mixed_evidence"
        reasons.append(
            "Configured descriptive criterion; not a validated probability or forecast."
        )
    return {
        "label": label,
        "reasons": reasons,
        "eligible_form_ids": [f["id"] for f in eligible],
        "recent_median": median(scores) if len(scores) >= minimum else None,
        "observed_range": [min(scores), max(scores)] if scores else None,
        "minimum_forms": minimum,
        "horizon_days": horizon,
        "score_prediction": None,
    }
