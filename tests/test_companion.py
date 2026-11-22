"""Behavioral fixtures are entirely synthetic and never use the learner's credentials."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import blueprint_report
from companion import ingest, items, jobs, lessons, metrics, reports, sync
from companion.service import Service
from companion.store import Conflict, Store
from study_sync import digest, timestamp
from tests.report_fixtures import activity, bundle, question

TARGET = {
    "section": "CP",
    "concept": "Synthetic proportional reasoning",
    "skill": "apply a ratio",
}


def test_item(purpose="diagnostic", suffix="1"):
    return {
        "id": "synthetic-" + purpose + suffix,
        "content_version": 1,
        "family": purpose + suffix,
        "target": TARGET,
        "purpose": purpose,
        "learner_view": {
            "stimulus": "Synthetic data: a constant rate is 2 units per second.",
            "question": "How many units accumulate in 3 seconds?",
            "options": {"A": "5", "B": "6"},
        },
        "instructor_view": {
            "key": "B",
            "explanation": "Multiply the rate by elapsed time: 2 times 3 equals 6 units.",
            "hints": ["Use rate times time."],
            "rubric": "Correct fixed-choice selection. Reasoning is retained separately.",
            "source_claims": [],
        },
        "synthetic": True,
        "provenance": {"author": "synthetic_test_fixture"},
    }


def install_bank(store, suffix="1"):
    with store.transaction():
        for purpose in (
            "diagnostic",
            "immediate_application",
            "delayed_recall",
            "delayed_transfer",
        ):
            item = test_item(purpose, suffix)
            review = {
                "learner_view_hash": digest(item["learner_view"]),
                "decision": "accept_for_instruction",
                "blinded_answer": "B",
                "key_withheld": True,
                "reviewer": "synthetic_fixture",
                "checks": {k: True for k in items.RELEASE_CHECKS},
            }
            items.install(store, item, review)


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "companion.sqlite"
        self.now = timestamp("2026-10-24T15:00:00Z")
        self.store = Store(self.path, clock=lambda: self.now)
        self.svc = Service(self.store)
        self.svc.preferences("setup", {"timezone": "America/New_York"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def start(self, slot="manual"):
        offer = self.svc.offer("offer-" + slot, slot)
        return self.svc.start("start-" + slot, offer["offer_id"], "closed_book")

    def answer(self, view, key="answer", help="none"):
        return self.svc.answer(
            key,
            session_id=view["session_id"],
            expected=view["version"],
            item_ref=view["item_ref"],
            item_version=view["item_version"],
            answer="B",
            reasoning="The rate multiplies elapsed time.",
            external_help=help,
            prior_exposure="new",
        )

    def test_capture_retries_preserve_details_no_duplicate_attempts(self):
        raw = bundle()
        for _ in range(3):
            ingest.ingest(self.store, raw)
        self.assertEqual(len(self.store.list("attempts")), 8)
        first = self.store.list("attempts")[0]
        self.assertIn("tags", first)
        self.assertIn("group_id", first)
        self.assertIn("raw_aggregate", first)
        self.assertEqual(first["prior_exposure"], "unknown")
        # A later capture of the exact same activity is an observation, not another attempt.
        raw["captured_at"] = "2026-10-23T19:00:00Z"
        self.assertEqual(ingest.ingest(self.store, raw)["status"], "accepted")
        self.assertEqual(len(self.store.list("attempts")), 8)

    def test_legacy_replay_preserves_enriched_report_at_same_revision(self):
        report = bundle()
        ingest.ingest(self.store, report)
        before = self.store.list("attempts")
        assessment = self.store.list("assessments")

        replay = blueprint_report.normalize(report)
        result = ingest.ingest(self.store, replay)

        self.assertEqual(result["unchanged"], len(before) + len(assessment))
        self.assertEqual(self.store.list("attempts"), before)
        self.assertEqual(self.store.list("assessments"), assessment)
        self.assertEqual(len(self.store.list("capture")), 2)

    def test_same_revision_can_gain_details_but_conflict_blocks_whole_capture(self):
        report = bundle()
        legacy = blueprint_report.normalize(report)
        ingest.ingest(self.store, legacy)
        before = self.store.list("attempts")
        # One incompatible row blocks enrichment of the other seven.
        changed = bundle()
        question(changed)["flagged"] = True
        question(changed, "identity")["flagged"] = True
        self.assertEqual(ingest.ingest(self.store, changed)["status"], "quarantined")
        self.assertEqual(self.store.list("attempts"), before)

        ingest.ingest(self.store, report)
        for row in self.store.list("attempts"):
            self.assertTrue(row["complete_scope"])
            self.assertIn("group_id", row)
            self.assertEqual(row["version"], 2)

    def test_same_timestamp_conflict_is_quarantined(self):
        raw = bundle()
        ingest.ingest(self.store, raw)
        question(raw)["flagged"] = True
        question(raw, "identity")["flagged"] = True
        self.assertEqual(ingest.ingest(self.store, raw)["status"], "quarantined")
        self.assertFalse(any(r["bookmarked"] for r in self.store.list("attempts")))

    def test_retake_distinct_activity_and_source_owner_guard(self):
        raw = bundle()
        ingest.ingest(self.store, raw)
        raw["activity_id"] = "synthetic-retake"
        for kind in ("score", "identity"):
            activity(raw, kind)["activityId"] = "synthetic-retake"
        ingest.ingest(self.store, raw)
        self.assertEqual(len(self.store.list("attempts")), 16)
        raw["learner_id"] = "different-person"
        with self.assertRaises(ValueError):
            ingest.ingest(self.store, raw)

    def test_metrics_omission_and_partial_denominators(self):
        rows = [
            {
                "id": str(i),
                "question_id": str(i),
                "outcome": "correct"
                if i < 40
                else "incorrect"
                if i < 55
                else "omitted",
                "complete_scope": True,
            }
            for i in range(59)
        ]
        result = metrics.performance(rows)
        self.assertEqual(result["answered_accuracy"], metrics.rate(40, 55))
        self.assertEqual(result["scored_accuracy"], metrics.rate(40, 59))
        self.assertEqual(result["fresh_independent_accuracy"]["denominator"], 0)
        for row in rows[:20]:
            row["complete_scope"] = False
        self.assertIsNone(metrics.performance(rows[:20])["scored_accuracy"]["value"])

    def test_dst_calendar_boundaries_nonoverlapping(self):
        spring = reports.period("daily", "2026-03-09T12:00:00Z", "America/New_York")
        fall = reports.period("daily", "2026-11-02T12:00:00Z", "America/New_York")
        self.assertEqual(
            (timestamp(spring["end"]) - timestamp(spring["start"])).total_seconds(),
            23 * 3600,
        )
        self.assertEqual(
            (timestamp(fall["end"]) - timestamp(fall["start"])).total_seconds(),
            25 * 3600,
        )
        month = reports.period("monthly", "2026-03-01T13:30:00Z", "America/New_York")
        self.assertEqual(month["local_start"][:10], "2026-02-01")
        self.assertEqual(month["comparison_end"], month["start"])

    def test_reports_cutoff_late_data_immutable_versions(self):
        # Source occurrence is Oct 22, ingestion is Oct 24.
        earlier = self.svc.report_context(
            "daily", "2026-10-23T15:00:00Z", "America/New_York"
        )
        first = self.svc.save_report("report-1", earlier["snapshot_id"])
        ingest.ingest(self.store, bundle())
        historical = self.svc.report_context(
            "daily", "2026-10-23T15:00:00Z", "America/New_York"
        )
        self.assertEqual(historical["current"], [])
        current = self.svc.report_context("daily")
        self.assertEqual(current["current"], [])  # Import is not yesterday's activity.
        with self.assertRaises(ValueError):
            self.svc.save_report(
                "bad", earlier["snapshot_id"], "Your predicted score is 520."
            )
        # Same period with later available data requires an explained supersession.
        revised = self.svc.report_context("daily", period_as_of="2026-10-23T15:00:00Z")
        self.assertEqual(sum(r["attempts"] for r in revised["current"]), 8)
        second = self.svc.save_report(
            "report-2",
            revised["snapshot_id"],
            correction_reason="Synthetic correction test",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            self.svc.get_report(first["id"], 1)["narrative"], first["narrative"]
        )

    def test_commit_is_durable_and_idempotent_before_feedback(self):
        install_bank(self.store)
        view = self.start()
        self.assertNotIn("instructor_view", json.dumps(view))
        result = self.answer(view)
        retry = self.answer(view)
        self.assertEqual(result, retry)
        self.assertEqual(len(self.store.list("lesson_attempt")), 1)
        with self.assertRaises(Conflict):
            self.answer(view, "different-key")
        self.store.close()
        self.store = Store(self.path, clock=lambda: self.now)
        self.svc = Service(self.store)
        self.assertEqual(len(self.store.list("lesson_attempt")), 1)
        paused = self.svc.lesson_state(
            "pause", view["session_id"], result["version"], "pause"
        )
        self.assertEqual(paused["state"], "paused")
        resumed = self.svc.start("resume", view["session_id"])
        self.assertNotEqual(view["item_ref"], resumed["item_ref"])

    def test_hint_assisted_and_versioned(self):
        install_bank(self.store)
        view = self.start()
        hint = self.svc.hint(
            "hint", view["session_id"], view["version"], view["item_ref"]
        )
        with self.assertRaises(Conflict):
            self.answer(view)
        view["version"] = hint["version"]
        result = self.answer(view)
        self.assertEqual(result["help"], "assisted")
        self.assertEqual(
            metrics.performance(self.store.list("lesson_attempt"))[
                "fresh_independent_accuracy"
            ]["denominator"],
            0,
        )

    def test_offer_expiry_neutral_bounded_and_skip(self):
        install_bank(self.store)
        offers = [self.svc.offer("offer" + str(i), "slot" + str(i)) for i in range(4)]
        self.assertEqual(
            sum(o["state"] == "offered" for o in self.store.list("offer")), 2
        )
        self.assertEqual(len(self.store.list("lesson_attempt")), 0)
        self.svc.skip("skip", offers[-1]["offer_id"])
        self.now += timedelta(days=2)
        with self.store.transaction():
            lessons.expire(self.store)
        self.assertEqual(
            sum(o["state"] == "offered" for o in self.store.list("offer")), 0
        )
        self.assertEqual(self.store.list("evidence_state"), [])

    def test_full_lesson_delayed_recall_and_transfer(self):
        install_bank(self.store)
        view = self.start()
        self.answer(view)
        next_view = self.svc.start("next", view["session_id"])
        second = self.answer(next_view, "answer2")
        finished = self.svc.lesson_state(
            "finish", view["session_id"], second["version"], "finish"
        )
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(len(finished["repair_ids"]), 2)
        self.now += timedelta(days=2, hours=1)
        check = self.start("delayed")
        response = self.answer(check, "delayed-answer")
        self.svc.lesson_state(
            "finish-check", check["session_id"], response["version"], "finish"
        )
        result = self.store.list("probe_result")[0]
        self.assertTrue(result["eligible"])
        self.assertTrue(result["in_window"])
        self.assertEqual(
            self.store.list("evidence_state")[0]["state"], "provisionally_demonstrated"
        )

    def test_repair_cohort_untested_not_failed(self):
        repairs = [
            {"id": str(i), "window_end": "2026-10-01T00:00:00Z"} for i in range(8)
        ]
        results = [
            {
                "repair_id": str(i),
                "eligible": True,
                "in_window": True,
                "passed": i < 4,
                "occurred_at": "2026-09-30T00:00:00Z",
            }
            for i in range(6)
        ]
        result = metrics.repair_metrics(repairs, results, self.store.now())
        self.assertEqual(result["verified_matured"], metrics.rate(4, 8))
        self.assertEqual(result["tested_matured"], metrics.rate(6, 8))
        self.assertEqual(result["passed_tested"], metrics.rate(4, 6))

    def test_dispute_removes_grade_from_current_metrics_keeps_history(self):
        install_bank(self.store)
        view = self.start()
        self.answer(view)
        self.svc.dispute("dispute", view["item_ref"], "Synthetic ambiguity")
        self.now += timedelta(days=1)
        ctx = self.svc.report_context("daily")
        self.assertEqual(
            sum(g["answered_accuracy"]["denominator"] for g in ctx["current"]), 0
        )
        self.assertEqual(len(self.store.list("lesson_attempt")), 1)

    def test_context_revision_stable_and_candidate_links(self):
        ingest.ingest(self.store, bundle())
        attempt = self.store.list("attempts")[0]
        entries = [
            {
                "id": "entry-a",
                "text": "I looked up this relationship.",
                "attempt_id": attempt["id"],
                "reason": "curiosity",
            }
        ]
        self.svc.upload_context(
            "upload", "history", "Question history", "Original text", entries=entries
        )
        self.svc.upload_context(
            "edit", "history", "Question history", "Edited text", 1, entries
        )
        self.assertEqual(len(self.store.list("context_entry")), 1)
        self.assertEqual(
            self.store.list("context_entry")[0]["link_status"], "candidate"
        )
        self.assertEqual(self.store.get("attempts", attempt["id"])["help"], "unknown")
        self.assertEqual(
            self.store.get("context_document", "history", version=1)["text"],
            "Original text",
        )

    def test_backup_restore_reproduces_responses_reports_and_retry_ack(self):
        install_bank(self.store)
        view = self.start()
        self.answer(view)
        ctx = self.svc.report_context("daily")
        report = self.svc.save_report("report", ctx["snapshot_id"])
        path = Path(self.tmp.name) / "backup.json"
        self.store.backup(path)
        restored = Store(
            Path(self.tmp.name) / "restored.sqlite", clock=lambda: self.now
        )
        try:
            restored.restore(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(restored.revision(), self.store.revision())
            self.assertEqual(Service(restored).get_report(report["id"]), report)
            self.assertEqual(len(restored.list("lesson_attempt")), 1)
        finally:
            restored.close()

    def test_readiness_preserves_provider_and_requires_conditions(self):
        forms = [
            {
                "id": str(i),
                "form_id": str(i),
                "provider": "aamc",
                "occurred_at": "2026-10-01T00:00:00Z",
                "prior_exposure": "confirmed_new",
                "help": "independent",
                "conditions_comparable": True,
                "complete": True,
                "scores": {"total": 512, "CP": 128, "CARS": 128, "BB": 128, "PS": 128},
            }
            for i in range(3)
        ]
        prefs = {"goals": {"total": 510, "CP": 127, "CARS": 127, "BB": 127, "PS": 127}}
        self.assertEqual(
            metrics.readiness(forms, prefs, self.store.now())["label"],
            "consistent_recent_demonstration",
        )
        forms[0]["provider"] = "blueprint"
        self.assertEqual(
            metrics.readiness(forms, prefs, self.store.now())["label"],
            "insufficient_evidence",
        )

    def test_scheduling_gate_and_lease_recovery(self):
        self.assertEqual(self.svc.due("due")["availability"], "schedules_disabled")
        with self.assertRaises(ValueError):
            self.svc.preferences(
                "unsafe", {"timezone": "America/New_York", "schedules_enabled": True}, 1
            )
        with self.store.transaction():
            for name in jobs.CAPABILITIES:
                jobs.record_capability(
                    self.store, name, "verified", "Synthetic test only"
                )
        self.svc.preferences(
            "enable", {"timezone": "America/New_York", "schedules_enabled": True}, 1
        )
        due = self.svc.due("due2")
        job_id = due["jobs"][0]["id"]
        with self.store.transaction():
            self.assertIsNotNone(jobs.claim(self.store, job_id, "a"))
            self.assertIsNone(jobs.claim(self.store, job_id, "b"))
        self.now += timedelta(minutes=6)
        with self.store.transaction():
            self.assertIsNotNone(jobs.claim(self.store, job_id, "b"))

    def test_sync_retry_and_changes_exclude_keys(self):
        raw = bundle()
        events = [{"event_id": digest(raw), "payload": raw}]
        sync.receive(self.store, events)
        sync.receive(self.store, events)
        install_bank(self.store)
        page = sync.changes(self.store)
        self.assertNotIn("instructor_view", json.dumps(page))
        self.assertEqual(len(self.store.list("attempts")), 8)

    def test_two_connections_submit_same_request_only_one_response(self):
        from concurrent.futures import ThreadPoolExecutor

        install_bank(self.store)
        view = self.start()

        def submit():
            own = Store(self.path, clock=lambda: self.now)
            try:
                return Service(own).answer(
                    "concurrent-answer",
                    session_id=view["session_id"],
                    expected=view["version"],
                    item_ref=view["item_ref"],
                    item_version=view["item_version"],
                    answer="B",
                )
            finally:
                own.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            answers = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(len(self.store.list("lesson_attempt")), 1)

    def test_report_retry_same_inputs_does_not_need_correction(self):
        first_ctx = self.svc.report_context("daily")
        first = self.svc.save_report("r1", first_ctx["snapshot_id"])
        self.now += timedelta(minutes=1)
        second_ctx = self.svc.report_context("daily")
        second = self.svc.save_report("r2", second_ctx["snapshot_id"])
        self.assertEqual(first, second)

    def test_abandon_paused_session_allows_another_optional_offer(self):
        install_bank(self.store)
        install_bank(self.store, "2")
        view = self.start()
        paused = self.svc.lesson_state(
            "pause", view["session_id"], view["version"], "pause"
        )
        ended = self.svc.lesson_state(
            "abandon", view["session_id"], paused["version"], "abandon"
        )
        self.assertEqual(ended["state"], "cancelled")
        offer = self.svc.offer("fresh-manual-offer")
        self.assertEqual(offer["state"], "offered")
        self.assertEqual(len(self.store.list("lesson_attempt")), 0)

    def test_late_delayed_check_preserves_original_window(self):
        install_bank(self.store)
        view = self.start()
        first = self.answer(view)
        finish = self.svc.lesson_state(
            "finish-partial", view["session_id"], first["version"], "finish"
        )
        original = self.store.get("repair", finish["repair_ids"][0])
        self.now += timedelta(days=5)
        late = self.start("late")
        self.answer(late, "late-answer")
        probe = self.store.list("probe_result")[0]
        self.assertTrue(probe["eligible"])
        self.assertFalse(probe["in_window"])
        self.assertEqual(
            self.store.get("repair", original["id"])["window_end"],
            original["window_end"],
        )

    def test_accidentally_exposed_probe_is_not_selected(self):
        install_bank(self.store)
        view = self.start()
        response = self.answer(view)
        self.svc.lesson_state(
            "finish-partial", view["session_id"], response["version"], "finish"
        )
        with self.store.transaction():
            self.store.put(
                "exposure",
                "leak",
                {
                    "family": "delayed_recall1",
                    "type": "accidental_preview",
                    "occurred_at": self.store.now(),
                },
            )
        self.now += timedelta(days=2, hours=1)
        offer = self.svc.offer("due-probe", "probe")
        # Recall unavailable; the known exposed family must never be returned.
        if "offer_id" in offer:
            check = self.svc.start("probe-start", offer["offer_id"], "closed_book")
            self.assertNotIn("delayed_recall", check.get("item_ref", ""))

    def test_unknown_and_categorical_confidence_are_not_probabilities(self):
        rows = [
            {
                "id": "a",
                "question_id": "a",
                "outcome": "correct",
                "confidence": {
                    "scale": "categorical",
                    "value": "high",
                    "before_feedback": True,
                },
            }
        ]
        self.assertIsNone(metrics.performance(rows)["confidence_brier"]["value"])
        rows[0]["confidence"] = {
            "scale": "probability",
            "value": 0.8,
            "before_feedback": True,
        }
        self.assertAlmostEqual(
            metrics.performance(rows)["confidence_brier"]["value"], 0.04
        )

    def test_preferences_patch_preserves_quiet_hours(self):
        self.svc.preferences("quiet", {"quiet_start": "20:00"}, 1)
        result = self.svc.preferences("tz", {"timezone": "UTC"}, 2)
        self.assertEqual(result["quiet_start"], "20:00")

    def test_scheduled_reads_fallback_keeps_interactive_write_gate(self):
        with self.store.transaction():
            for name in jobs.CAPABILITIES:
                jobs.record_capability(
                    self.store,
                    name,
                    "failed" if name == "scheduled_write" else "verified",
                    "Synthetic test only",
                )
        result = self.svc.preferences(
            "read-mode",
            {
                "scheduled_delivery_mode": "backend_prepared_reads",
                "schedules_enabled": True,
            },
            1,
        )
        self.assertTrue(result["schedules_enabled"])
        self.assertTrue(jobs.ready(self.store))
        with self.store.transaction():
            jobs.record_capability(
                self.store, "interactive_write", "failed", "Synthetic failure"
            )
        self.assertFalse(jobs.ready(self.store))

    def test_non_synthetic_item_requires_claim_provenance(self):
        item = test_item()
        item["synthetic"] = False
        review = {
            "learner_view_hash": digest(item["learner_view"]),
            "decision": "accept_for_instruction",
            "blinded_answer": "B",
            "key_withheld": True,
            "reviewer": "synthetic_test",
            "checks": {k: True for k in items.RELEASE_CHECKS},
        }
        with self.store.transaction():
            result = items.install(self.store, item, review)
        self.assertEqual(result["status"], "needs_review")

    def test_unreviewed_bank_is_not_offered(self):
        item = test_item()
        review = {
            "learner_view_hash": digest(item["learner_view"]),
            "decision": "revise",
        }
        with self.store.transaction():
            items.install(self.store, item, review)
        self.assertEqual(
            self.svc.offer("no-bank")["availability"], "reviewed_bank_exhausted"
        )

    def test_successful_transfer_changes_later_recommendation(self):
        from companion.policy import candidates

        with self.store.transaction():
            self.store.put(
                "attempts",
                "old-error",
                {
                    "section": TARGET["section"],
                    "topic": TARGET["concept"],
                    "skill": TARGET["skill"],
                    "occurred_at": "2026-10-20T12:00:00Z",
                    "outcome": "incorrect",
                },
            )
            self.store.put(
                "repair",
                "repair-a",
                {
                    "target": TARGET,
                    "purpose": "delayed_transfer",
                    "due_at": "2026-10-22T12:00:00Z",
                    "origin_evidence_ids": ["old-error"],
                    "pass_criterion": "Independent new application",
                },
            )
            self.store.put(
                "probe_result",
                "check-a",
                {
                    "repair_id": "repair-a",
                    "purpose": "delayed_transfer",
                    "eligible": True,
                    "passed": True,
                    "occurred_at": "2026-10-23T12:00:00Z",
                },
            )
        self.assertEqual(candidates(self.store), [])


if __name__ == "__main__":
    unittest.main()
