"""Optional offers, committed responses, bounded hints, and delayed evidence."""

from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from companion import POLICY_VERSION
from companion.models import Confidence
from companion.policy import candidates, choose_items
from companion.store import Conflict
from study_sync import digest, iso, timestamp


def event(store, kind, session_id=None, **fields):
    return store.put(
        "lesson_event",
        str(uuid4()),
        {
            "type": kind,
            "session_id": session_id,
            "occurred_at": store.now(),
            "timestamp_precision": "server_received",
            **fields,
        },
    )


def expire(store):
    for offer in store.list("offer"):
        if (
            offer["state"] == "offered"
            and timestamp(offer["expires_at"]) <= store.clock()
        ):
            store.put("offer", offer["id"], {**offer, "state": "expired"})
            event(store, "expired", offer_id=offer["id"])


def offer(store, slot="manual", target=None):
    expire(store)
    prefs = store.get("preferences", "current") or {}
    if not prefs.get("timezone"):
        raise ValueError("Set the learner timezone before offering lessons")
    local = store.clock().astimezone(ZoneInfo(prefs["timezone"]))
    identifier = digest([store.learner, local.date().isoformat(), slot])
    existing = store.get("offer", identifier)
    if existing:
        return public_offer(existing)
    active = [s for s in store.list("session") if s["state"] in {"started", "paused"}]
    if active:
        return {"availability": "active_session", "session_id": active[0]["id"]}
    todays = [
        o for o in store.list("offer") if o["local_date"] == local.date().isoformat()
    ]
    if len(todays) >= prefs.get("daily_cap", 4):
        return {"availability": "daily_cap"}
    clock = local.strftime("%H:%M")
    start, end = prefs.get("quiet_start", "21:00"), prefs.get("quiet_end", "08:00")
    quiet = (
        start <= clock < end
        if start < end
        else clock >= start or clock < end
        if start != end
        else False
    )
    if quiet and not slot.startswith("manual"):
        return {"availability": "quiet_hours"}
    options = candidates(store)
    if target:
        from companion.models import Target

        target = Target.model_validate(target).model_dump()
        options.insert(
            0,
            {
                "priority": 0,
                "target": target,
                "purpose": "diagnostic",
                "repair_id": None,
                "evidence_ids": [],
                "reason": "The learner explicitly requested this objective.",
                "verification_criterion": "New independent delayed application.",
                "estimated_minutes": 7,
            },
        )
    # Known strengths / coverage probes can be offered without declaring unknown topics weak.
    for item in store.list("item"):
        if item["status"] == "accepted" and item["purpose"] == "diagnostic":
            options.append(
                {
                    "priority": 4,
                    "target": item["target"],
                    "purpose": "diagnostic",
                    "repair_id": None,
                    "evidence_ids": [],
                    "reason": "A coverage probe can clarify currently unknown evidence.",
                    "verification_criterion": "New independent delayed application.",
                    "estimated_minutes": 7,
                }
            )
    selected, refs = None, []
    for candidate in options:
        refs = choose_items(store, candidate, store.now())
        if refs:
            challenge = any(store.get("item", r)["challenge_intent"] for r in refs)
            if challenge and any(o.get("challenge_intent") for o in todays):
                continue
            selected = candidate
            break
    if not selected:
        return {
            "availability": "reviewed_bank_exhausted",
            "message": "No eligible reviewed lesson is available.",
        }
    current = sorted(
        [o for o in store.list("offer") if o["state"] == "offered"],
        key=lambda o: o["created_at"],
    )
    while len(current) >= prefs.get("max_current_offers", 2):
        old = current.pop(0)
        store.put(
            "offer", old["id"], {**old, "state": "expired", "expiry_reason": "replaced"}
        )
        event(store, "expired", offer_id=old["id"])
    decision = store.put(
        "policy_decision",
        identifier,
        {"candidates": options, "selected": selected, "policy_version": POLICY_VERSION},
    )
    result = store.put(
        "offer",
        identifier,
        {
            "state": "offered",
            "created_at": store.now(),
            "local_date": local.date().isoformat(),
            "slot": slot,
            "expires_at": iso(
                store.clock() + timedelta(hours=prefs.get("offer_expiry_hours", 24))
            ),
            "candidate": selected,
            "item_refs": refs,
            "decision_id": decision["id"],
            "challenge_intent": challenge,
        },
    )
    return public_offer(result)


def public_offer(offer):
    c = offer["candidate"]
    return {
        "offer_id": offer["id"],
        "version": offer["version"],
        "state": offer["state"],
        "title": c["target"]["concept"] + " practice",
        "estimated_minutes": c["estimated_minutes"],
        "reason": c["reason"],
        "expires_at": offer["expires_at"],
        "optional": True,
    }


def next_view(store, session):
    if session["position"] >= len(session["item_refs"]):
        return {
            "session_id": session["id"],
            "version": session["version"],
            "state": session["state"],
            "next": "reflection_or_finish",
            "message": "The attempted objective is not yet delayed verification.",
        }
    item = store.get("item", session["item_refs"][session["position"]])
    if item["status"] != "accepted":
        return {
            "session_id": session["id"],
            "version": session["version"],
            "next": "unscorable",
            "message": "The item is under review; no grade will be used.",
        }
    exposure_id = digest([session["id"], session["position"], "question"])
    if not store.get("exposure", exposure_id):
        prior = any(e["family"] == item["family"] for e in store.list("exposure"))
        store.put(
            "exposure",
            exposure_id,
            {
                "session_id": session["id"],
                "item_ref": item["id"],
                "family": item["family"],
                "type": "question",
                "prior_within_companion": prior,
                "occurred_at": store.now(),
            },
        )
    return {
        "session_id": session["id"],
        "version": session["version"],
        "state": session["state"],
        "item_ref": item["id"],
        "item_version": item["content_version"],
        "learner_view": item["learner_view"],
        "condition": session["condition"],
        "next": "answer",
    }


def start(store, offer_id, condition="unspecified"):
    if condition not in {"closed_book", "assisted", "unspecified"}:
        raise ValueError("Invalid session condition")
    expire(store)
    offer = store.get("offer", offer_id)
    if not offer:
        raise ValueError("Unknown offer")
    session = store.get("session", offer_id)
    if session:
        if session["state"] == "paused":
            session = store.put(
                "session", session["id"], {**session, "state": "started"}
            )
            event(store, "resumed", session["id"])
        return next_view(store, session)
    if offer["state"] != "offered":
        raise Conflict("Offer is no longer current; request a new optional offer")
    if any(s["state"] in {"started", "paused"} for s in store.list("session")):
        raise Conflict("Resume or finish the current lesson first")
    c = offer["candidate"]
    refs = choose_items(store, c, store.now())
    if c.get("repair_id") and any(
        r["repair_id"] == c["repair_id"] and r["eligible"] and r["passed"]
        for r in store.list("probe_result")
    ):
        refs = []
    if not refs:
        raise Conflict("Offer needs refreshing because eligibility or exposure changed")
    store.put("offer", offer_id, {**offer, "state": "started"})
    session = store.put(
        "session",
        offer_id,
        {
            "offer_id": offer_id,
            "state": "started",
            "position": 0,
            "item_refs": refs,
            "condition": condition,
            "condition_basis": "learner_report",
            "candidate": c,
            "created_at": store.now(),
            "hints": {},
            "responses": [],
        },
    )
    event(store, "started", session["id"])
    return next_view(store, session)


def get_current(store, session_id, expected, item_ref=None):
    session = store.get("session", session_id)
    if not session or session["version"] != expected:
        raise Conflict("Stale session version; refresh current state")
    if session["state"] != "started":
        raise Conflict("Resume the lesson before responding")
    if item_ref is not None and (
        session["position"] >= len(session["item_refs"])
        or session["item_refs"][session["position"]] != item_ref
    ):
        raise Conflict("This item is not the current lesson step")
    return session


def answer(
    store,
    session_id,
    expected,
    item_ref,
    item_version,
    answer,
    reasoning="",
    confidence=None,
    external_help="unknown",
    prior_exposure="unknown",
):
    session = get_current(store, session_id, expected, item_ref)
    item = store.get("item", item_ref)
    if item["content_version"] != item_version or item["status"] != "accepted":
        raise Conflict("Item is stale or unscorable")
    if answer not in item["learner_view"]["options"] and answer != "I don't know":
        raise ValueError("Commit an option key or explicit I don't know")
    if (
        len(reasoning) > 5000
        or external_help not in {"none", "used", "unknown"}
        or prior_exposure not in {"new", "seen", "unknown"}
    ):
        raise ValueError("Invalid response conditions")
    confidence = (
        Confidence.model_validate(confidence).model_dump() if confidence else None
    )
    shown = store.get("exposure", digest([session_id, session["position"], "question"]))
    if not shown:
        raise Conflict("Fetch the current question before committing an answer")
    assisted = (
        bool(session["hints"].get(item_ref))
        or external_help == "used"
        or session["condition"] == "assisted"
    )
    independent = not assisted and (
        external_help == "none" or session["condition"] == "closed_book"
    )
    fresh = prior_exposure == "new" and not shown["prior_within_companion"]
    correct = answer == item["instructor_view"]["key"]
    response = event(
        store,
        "answer_committed",
        session_id,
        item_ref=item_ref,
        item_version=item_version,
        exact_answer=answer,
        reasoning=reasoning,
        confidence=confidence,
        correct=correct,
        help="assisted" if assisted else "independent" if independent else "unknown",
        prior_exposure="confirmed_new"
        if fresh
        else "seen"
        if prior_exposure == "seen" or shown["prior_within_companion"]
        else "unknown",
        condition_basis="learner_report_and_companion_exposure",
        rubric=item["instructor_view"]["rubric"],
        reasoning_grade=None,
        grading_basis="server_fixed_key",
    )
    attempt = store.put(
        "lesson_attempt",
        response["id"],
        {
            **response,
            "question_id": item_ref,
            "item_ref": item_ref,
            "source": "synthetic_fixture" if item["synthetic"] else "generated_drill",
            "section": item["target"]["section"],
            "topic": item["target"]["concept"],
            "skill": item["target"]["skill"],
            "purpose": item["purpose"],
            "outcome": "correct" if correct else "incorrect",
            "complete_scope": True,
            "mode": "short_untimed",
            "challenge_intent": item["challenge_intent"],
        },
    )
    candidate = session["candidate"]
    if candidate.get("repair_id"):
        repair = store.get("repair", candidate["repair_id"])
        lag = (store.clock() - timestamp(repair["reviewed_at"])).total_seconds()
        eligible = (
            independent
            and fresh
            and item["family"] not in repair["excluded_families"]
            and store.clock() >= timestamp(repair["due_at"])
        )
        probe = store.put(
            "probe_result",
            attempt["id"],
            {
                "repair_id": repair["id"],
                "purpose": repair["purpose"],
                "item_ref": item_ref,
                "occurred_at": store.now(),
                "actual_lag_seconds": lag,
                "eligible": eligible,
                "in_window": timestamp(repair["due_at"])
                <= store.clock()
                <= timestamp(repair["window_end"]),
                "passed": correct,
                "response_id": response["id"],
            },
        )
        store.put(
            "evidence_state",
            digest(item["target"]),
            {
                "target": item["target"],
                "purpose": repair["purpose"],
                "state": "provisionally_demonstrated"
                if eligible and correct
                else "needs_recheck"
                if eligible
                else "learning",
                "evidence_ids": [probe["id"]],
                "policy_version": POLICY_VERSION,
            },
        )
    session = store.put(
        "session",
        session_id,
        {
            **session,
            "position": session["position"] + 1,
            "responses": [*session["responses"], response["id"]],
        },
    )
    store.put(
        "exposure",
        digest([response["id"], "feedback"]),
        {
            "session_id": session_id,
            "item_ref": item_ref,
            "family": item["family"],
            "type": "feedback",
            "occurred_at": store.now(),
        },
    )
    return {
        "saved": True,
        "response_id": response["id"],
        "session_id": session_id,
        "version": session["version"],
        "correct": correct,
        "grading_basis": "server_fixed_key",
        "reasoning_grade": "ungraded",
        "feedback": item["instructor_view"]["explanation"],
        "help": response["help"],
        "next": "fetch_next_step"
        if session["position"] < len(session["item_refs"])
        else "reflection_or_finish",
    }


def hint(store, session_id, expected, item_ref):
    session = get_current(store, session_id, expected, item_ref)
    item = store.get("item", item_ref)
    if item["status"] != "accepted":
        raise Conflict("Item is under review")
    count = session["hints"].get(item_ref, 0)
    hints = item["instructor_view"]["hints"]
    if count >= len(hints):
        return {"available": False, "version": session["version"]}
    event(store, "hint", session_id, item_ref=item_ref)
    session = store.put(
        "session",
        session_id,
        {**session, "hints": {**session["hints"], item_ref: count + 1}},
    )
    return {
        "available": True,
        "hint": hints[count],
        "version": session["version"],
        "assisted": True,
    }


def state(store, session_id, expected, action):
    session = store.get("session", session_id)
    if (
        not session
        or session["version"] != expected
        or session["state"] not in {"started", "paused"}
    ):
        raise Conflict(
            "Refresh the active/paused session before changing participation state"
        )
    if action not in {"pause", "finish", "abandon"}:
        raise ValueError("Use pause, finish, or abandon")
    if action == "finish" and not session["responses"]:
        raise ValueError(
            "No objective attempted; pause rather than recording completion"
        )
    completed = session["position"] == len(session["item_refs"])
    status = (
        "paused"
        if action == "pause"
        else ("partial" if session["responses"] else "cancelled")
        if action == "abandon"
        else "completed"
        if completed
        else "partial"
    )
    session = store.put("session", session_id, {**session, "state": status})
    event(store, status, session_id)
    repair_ids = []
    if action == "finish" and not session["candidate"].get("repair_id"):
        for purpose in ("delayed_recall", "delayed_transfer"):
            identifier = digest([session_id, purpose])
            repair = store.put(
                "repair",
                identifier,
                {
                    "target": session["candidate"]["target"],
                    "purpose": purpose,
                    "reviewed_at": store.now(),
                    "due_at": iso(store.clock() + timedelta(days=2)),
                    "window_end": iso(store.clock() + timedelta(days=3)),
                    "origin_evidence_ids": session["responses"],
                    "excluded_families": [
                        store.get("item", r)["family"] for r in session["item_refs"]
                    ],
                    "pass_criterion": "First eligible fixed-choice answer correct, with learner-reported independence and new exposure.",
                    "policy_version": POLICY_VERSION,
                    "pilot_window": True,
                },
            )
            repair_ids.append(repair["id"])
    return {
        "saved": True,
        "state": status,
        "session_id": session_id,
        "version": session["version"],
        "repair_ids": repair_ids,
        "mastery_claim": None,
    }
