"""Explicitly reported form history, kept separate by provider and conditions."""

from study_sync import timestamp


def record(store, data, expected=0):
    required = {
        "id",
        "form_id",
        "provider",
        "occurred_at",
        "scores",
        "prior_exposure",
        "help",
        "conditions_comparable",
        "complete",
        "conditions",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError(
            "Assessment fields must match the documented form-history contract"
        )
    if not all(
        isinstance(data[k], str) and 1 <= len(data[k]) <= 300
        for k in ("id", "form_id", "provider")
    ):
        raise ValueError("Invalid assessment identity")
    timestamp(data["occurred_at"])
    if timestamp(data["occurred_at"]) > store.clock():
        raise ValueError("Assessment occurrence cannot be in the future")
    if data["help"] not in {"independent", "assisted", "unknown"} or data[
        "prior_exposure"
    ] not in {"confirmed_new", "seen", "unknown"}:
        raise ValueError("Invalid assessment conditions")
    if (
        type(data["conditions_comparable"]) is not bool
        or type(data["complete"]) is not bool
        or not isinstance(data["conditions"], str)
    ):
        raise ValueError("Describe timing, accommodations, breaks and condition limits")
    if not isinstance(data["scores"], dict) or set(data["scores"]) - {
        "total",
        "CP",
        "CARS",
        "BB",
        "PS",
    }:
        raise ValueError("Invalid score fields")
    for key, value in data["scores"].items():
        if value is not None and (
            type(value) is not int
            or not ((472 <= value <= 528) if key == "total" else (118 <= value <= 132))
        ):
            raise ValueError(
                "Only reported MCAT-scale scores are accepted; never convert raw percentages"
            )
    return store.put(
        "assessment",
        data["id"],
        {
            **data,
            "condition_basis": "learner_report",
            "scale": "provider_reported_mcat_scale",
        },
        expected=expected,
    )
