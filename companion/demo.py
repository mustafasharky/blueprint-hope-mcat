"""Run a synthetic full lesson and delayed check in an isolated, temporary ledger."""

import json
import tempfile
from datetime import timedelta
from pathlib import Path

from companion.items import RELEASE_CHECKS, install
from companion.service import Service
from companion.store import Store
from study_sync import digest, timestamp


def main():
    with tempfile.TemporaryDirectory(prefix="blueprint-companion-demo-") as tmp:
        now = [timestamp("2026-10-24T15:00:00Z")]
        store = Store(Path(tmp) / "demo.sqlite", clock=lambda: now[0])
        try:
            svc = Service(store)
            svc.preferences("setup", {"timezone": "UTC"})
            for index, purpose in enumerate(
                (
                    "diagnostic",
                    "immediate_application",
                    "delayed_recall",
                    "delayed_transfer",
                )
            ):
                rate, duration = index + 2, index + 3
                item = {
                    "id": "synthetic-" + purpose,
                    "content_version": 1,
                    "family": "synthetic-family-" + str(index),
                    "target": {
                        "section": "CP",
                        "concept": "Synthetic rate calculation",
                        "skill": "multiply rate by time",
                    },
                    "purpose": purpose,
                    "synthetic": True,
                    "learner_view": {
                        "stimulus": "Synthetic constant-rate data.",
                        "question": f"A quantity grows at {rate} units per second. How many units accumulate in {duration} seconds?",
                        "options": {
                            "A": str(rate * duration),
                            "B": str(rate + duration),
                        },
                    },
                    "instructor_view": {
                        "key": "A",
                        "explanation": f"Rate multiplied by time gives {rate * duration} units.",
                        "hints": ["Multiply the rate by elapsed time."],
                        "rubric": "Correct fixed-choice calculation.",
                        "source_claims": [],
                    },
                    "provenance": {
                        "author": "deterministic_synthetic_demo",
                        "purpose": "software_validation_only",
                    },
                }
                review = {
                    "learner_view_hash": digest(item["learner_view"]),
                    "decision": "accept_for_instruction",
                    "key_withheld": True,
                    "blinded_answer": "A",
                    "reviewer": "synthetic_software_fixture",
                    "checks": {k: True for k in RELEASE_CHECKS},
                }
                with store.transaction():
                    install(store, item, review)
            offer = svc.offer("offer")
            view = svc.start("start", offer["offer_id"], "closed_book")
            for index in range(2):
                result = svc.answer(
                    "answer-" + str(index),
                    session_id=view["session_id"],
                    expected=view["version"],
                    item_ref=view["item_ref"],
                    item_version=view["item_version"],
                    answer="A",
                    reasoning="Synthetic response: rate times time.",
                    external_help="none",
                    prior_exposure="new",
                )
                if not index:
                    view = svc.start("next", view["session_id"])
            svc.lesson_state("finish", view["session_id"], result["version"], "finish")
            now[0] += timedelta(days=2, hours=1)
            followup = svc.offer("followup", "manual-check")
            view = svc.start("followup-start", followup["offer_id"], "closed_book")
            svc.answer(
                "followup-answer",
                session_id=view["session_id"],
                expected=view["version"],
                item_ref=view["item_ref"],
                item_version=view["item_version"],
                answer="A",
                external_help="none",
                prior_exposure="new",
            )
            now[0] += timedelta(days=2)
            ctx = svc.report_context("weekly", period_as_of="2026-10-26T12:00:00Z")
            report = svc.save_report("report", ctx["snapshot_id"])
            print(
                json.dumps(
                    {
                        "synthetic_only": True,
                        "persisted_answers": len(store.list("lesson_attempt")),
                        "delayed_checks": len(store.list("probe_result")),
                        "saved_report_version": report["version"],
                        "lesson_states": [s["state"] for s in store.list("session")],
                        "note": "Temporary demo ledger; no uploads, credentials or billable API calls.",
                    },
                    indent=2,
                )
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
