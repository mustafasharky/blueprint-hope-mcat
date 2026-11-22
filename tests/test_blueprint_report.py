"""Synthetic responses only. No real learner IDs, passages, or performance."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import blueprint_report as report
import study_sync as store
from tests.report_fixtures import CAPTURED, LAST, QUERY, START, activity, bundle, question, raw_pair, request


class AdapterTests(unittest.TestCase):
    def test_completed_report_and_null_placeholders(self):
        data = report.normalize(bundle())
        self.assertEqual(len(data["attempts"]), 8)
        self.assertEqual(sum(r["correct"] for r in data["attempts"]), 4)
        self.assertEqual(sum(r["seconds"] for r in data["attempts"]), 84)
        self.assertEqual(data["assessments"][0]["scores"]["total"], 486)
        self.assertEqual(data["coverage"], dict(attempts="partial", assessments="partial", activities="unavailable"))
        self.assertEqual(data["attempts"][0]["occurred_at"], START)
        self.assertEqual(data["attempts"][0]["updated_at"], LAST)

    def test_projection_discards_question_content_and_secrets(self):
        score, identity = raw_pair()
        score["data"]["token"] = "SECRET"
        score["data"]["student"]["email"] = "UNWANTED EMAIL"
        actual = report.make_bundle(score, identity, "DEMO_ONLY", "demo-activity", CAPTURED)
        self.assertNotIn("UNWANTED", json.dumps(actual))
        self.assertNotIn("SECRET", json.dumps(actual))

    def test_multi_topic_keeps_one_attempt_and_overlapping_summaries(self):
        b = bundle()
        question(b)["question"]["metadata"].append(
            {"id": "demo-other-topic", "title": "Another synthetic topic", "displayName": "Topic"})
        data = report.normalize(b)
        with tempfile.TemporaryDirectory() as tmp:
            db = store.connect(Path(tmp) / "study.sqlite")
            try:
                store.ingest(db, data)
                exported = store.export_state(db)
            finally:
                db.close()
        self.assertEqual(len(exported["records"]["attempts"]), 8)
        summaries = [s for s in exported["topic_summaries"] if s["section"] == "CP"]
        self.assertEqual(sorted(s["windows"]["all"]["attempts"] for s in summaries), [1, 2])

    def test_retake_uses_activity_not_position_for_attempt_id(self):
        first = bundle()
        second = copy.deepcopy(first)
        second["activity_id"] = "demo-retake"
        for kind in ("score", "identity"):
            activity(second, kind)["activityId"] = "demo-retake"
        a, b = report.normalize(first), report.normalize(second)
        self.assertEqual({q["question_id"] for q in a["attempts"]}, {q["question_id"] for q in b["attempts"]})
        self.assertFalse({q["id"] for q in a["attempts"]} & {q["id"] for q in b["attempts"]})

    def test_mismatches_and_missing_ids_fail_closed(self):
        for field, value in (("answer", ["different"]), ("previousAnswer", ["different"]), ("flagged", True)):
            b = bundle()
            question(b, "identity")[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                report.normalize(b)
        b = bundle()
        del question(b, "identity")["question"]["id"]
        with self.assertRaises(ValueError):
            report.normalize(b)

    def test_count_loss_and_duplicate_id_rejected(self):
        for kind in ("score", "identity"):
            b = bundle()
            activity(b, kind)["sections"][0]["questionGroups"][0]["questions"].pop()
            with self.assertRaises(ValueError):
                report.normalize(b)
        b = bundle()
        qs = activity(b, "identity")["sections"][0]["questionGroups"][0]["questions"]
        qs[1]["question"]["id"] = qs[0]["question"]["id"]
        with self.assertRaises(ValueError):
            report.normalize(b)

    def test_incomplete_aggregate_and_graphql_errors_rejected(self):
        b = bundle()
        activity(b)["aggregate"]["correct"] += 1
        with self.assertRaises(ValueError):
            report.normalize(b)
        b = bundle()
        for kind in ("score", "identity"):
            activity(b, kind)["aggregate"]["isComplete"] = False
        with self.assertRaises(ValueError):
            report.normalize(b)
        b = bundle()
        b["score"]["errors"] = [{"message": "partial response"}]
        with self.assertRaises(ValueError):
            report.normalize(b)

    def test_question_last_seen_used_when_end_time_null(self):
        b = bundle()
        self.assertIsNone(question(b)["aggregate"]["endTime"])
        normalized = report.normalize(b)
        self.assertEqual({r["updated_at"] for r in normalized["attempts"]}, {LAST})
        question(b)["aggregate"]["startTime"] = None
        with self.assertRaises(ValueError):
            report.normalize(b)

    def test_pipeline_repeat_preserves_observations_and_failed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, out = Path(tmp) / "study.sqlite", Path(tmp) / "state.json"
            self.assertEqual(report.sync_bundle(bundle(), db_path, out)["inserted"], 9)
            db = store.connect(db_path)
            try:
                store.record_observation(db, {"id": "demo-note", "occurred_at": LAST,
                    "topic": "Synthetic topic", "kind": "misconception", "text": "Synthetic note",
                    "source": "tutor_observation", "evidence_ids": []})
            finally:
                db.close()
            self.assertEqual(report.sync_bundle(bundle(), db_path, out)["status"], "already_imported")
            exported = report.read_json(out)
            self.assertEqual(len(exported["records"]["attempts"]), 8)
            self.assertEqual(len(exported["local_observations"]), 1)
            self.assertEqual(store.timestamp(exported["source_captured_at"]), store.timestamp(CAPTURED))
            before = out.read_bytes()
            broken = bundle()
            question(broken)["aggregate"]["correct"] = 10
            with self.assertRaises(ValueError):
                report.sync_bundle(broken, db_path, out)
            self.assertEqual(out.read_bytes(), before)

    def test_topic_validation(self):
        data = report.normalize(bundle())
        for invalid in ([], [""], ["Synthetic topic", "Synthetic topic"], "Synthetic topic"):
            data["attempts"][0]["topics"] = invalid
            with self.assertRaises(ValueError):
                store.validate_snapshot(data)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_and_operation_boundaries(self):
        req = request()
        self.assertTrue(report.request_matches(req, "demo-activity"))
        self.assertFalse(report.request_matches(req, "another-activity"))
        for bad_url in (report.API+"?token=secret", report.API.replace(".com", ".com.evil.test")):
            req.url = bad_url
            self.assertFalse(report.request_matches(req, "demo-activity"))
        req = request()
        req.post_data_json["query"] = QUERY.replace("query", "mutation", 1)
        self.assertFalse(report.request_matches(req, "demo-activity"))
        req.post_data_json = {"query": "query { login { token } }", "variables": {"activityId": "demo-activity"}}
        self.assertFalse(report.request_matches(req, "demo-activity"))

    async def test_two_graphql_operations_do_not_overwrite_each_other(self):
        collector = report.ReportCollector("demo-activity")
        for body in raw_pair():
            response = MagicMock(status=200)
            response.body = AsyncMock(return_value=json.dumps(body).encode())
            await collector.inspect(response)
        await collector.finish()
        self.assertEqual(set(collector.responses), {"score", "identity"})
        self.assertNotIn("UNWANTED", json.dumps(collector.responses))
        data = report.make_bundle(collector.responses["score"], collector.responses["identity"],
                                 "DEMO_ONLY", "demo-activity", max(collector.times.values()), collector.times)
        self.assertEqual(len(report.normalize(data)["attempts"]), 8)

    async def test_failed_response_cannot_become_empty_success(self):
        collector = report.ReportCollector("demo-activity")
        response = MagicMock(status=403)
        response.body = AsyncMock(return_value=b'{"secret":"must not print"}')
        await collector.inspect(response)
        with self.assertRaises(ValueError):
            await collector.finish()
        response.body.assert_not_called()
        self.assertNotIn("secret", json.dumps(collector.errors))


if __name__ == "__main__":
    unittest.main()
