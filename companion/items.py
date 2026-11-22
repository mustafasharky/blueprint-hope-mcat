"""Administrative content release. Protected content never enters report contexts."""

from companion.models import Item
from study_sync import digest

RELEASE_CHECKS = {
    "facts",
    "units",
    "unique_answer",
    "sufficient_stimulus",
    "skill_alignment",
    "no_answer_leakage",
    "novel_context",
}


def install(store, data, review):
    item = Item.model_validate(data).model_dump()
    reference = item["id"] + ":v" + str(item["content_version"])
    old = store.get("item", reference)
    if old:
        if old["content_hash"] != digest(item):
            raise ValueError(
                "Content versions are immutable; increment content_version"
            )
        return {"id": reference, "status": old["status"]}
    if review.get("learner_view_hash") != digest(item["learner_view"]):
        raise ValueError("Review must name the exact learner view hash")
    passed = review.get("decision") == "accept_for_instruction"
    passed &= review.get("blinded_answer") == item["instructor_view"]["key"]
    passed &= review.get("key_withheld") is True and bool(review.get("reviewer"))
    passed &= all(review.get("checks", {}).get(k) is True for k in RELEASE_CHECKS)
    if not item["synthetic"]:
        claims = item["instructor_view"]["source_claims"]
        passed &= bool(claims) and all(
            c.get("verified_at")
            and c.get("supporting_text")
            and (c.get("source_url") or c.get("source_kind") == "provided_stimulus")
            for c in claims
        )
    # A review is provenance, not a claim of empirical difficulty or psychometric validity.
    result = store.put(
        "item",
        reference,
        {
            **item,
            "content_hash": digest(item),
            "review": review,
            "status": "accepted" if passed else "needs_review",
        },
    )
    return {"id": reference, "status": result["status"]}


def quarantine(store, item_ref, reason):
    old = store.get("item", item_ref)
    if not old:
        raise ValueError("Unknown item version")
    if not reason.strip():
        raise ValueError("Provide the disputed key or ambiguity")
    result = store.put(
        "item", item_ref, {**old, "status": "quarantined", "dispute": reason[:5000]}
    )
    affected_results = {
        r["id"] for r in store.list("probe_result") if r.get("item_ref") == item_ref
    }
    for state in store.list("evidence_state"):
        if affected_results.intersection(state["evidence_ids"]):
            store.put(
                "evidence_state",
                state["id"],
                {
                    **state,
                    "state": "needs_recheck",
                    "revision_reason": "Supporting item quarantined",
                    "quarantined_item": item_ref,
                },
            )
    return {
        "id": item_ref,
        "status": result["status"],
        "affected_reports": [
            r["id"]
            for r in store.list("report")
            if any(
                a["id"] in r["evidence_ids"]
                for a in store.list("lesson_attempt")
                if a.get("item_ref") == item_ref
            )
        ],
        "action": "Future metrics exclude this item. Previously saved reports remain intact and can be superseded.",
    }
