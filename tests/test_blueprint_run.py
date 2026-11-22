import asyncio
import copy
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import blueprint_login as login
import blueprint_report as report
import blueprint_run as run
import study_sync as store
from tests.report_fixtures import bundle, activity, question, request, raw_pair, CAPTURED


def context_with(url):
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    context = MagicMock()
    context.pages = [page]
    return context, page


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_post_login_landing_titles_and_auth_pages(self):
        for title in ("My Account - Google Chrome", "Blueprint MCAT® - Dashboard - Google Chrome",
                      "Blueprint MCAT® - Score Report - Synthetic Exam - Google Chrome"):
            self.assertTrue(login.is_portal_title(title))
        for title in ("Sign In to Blueprint - Google Chrome", "Sign in - Google Accounts - Google Chrome",
                      "Blueprint MCAT® - Google Chrome"):
            self.assertFalse(login.is_portal_title(title))

    async def test_normal_browser_handoff_closes_only_a_stable_report_window(self):
        process = MagicMock(pid=123)
        process.poll.return_value = None
        windows = MagicMock()
        windows.titles.side_effect = [
            [(11, "Sign in - Google Accounts - Google Chrome")],
            [(12, "Blueprint MCAT® - Score Report - Synthetic Exam - Google Chrome")],
            [(12, "Blueprint MCAT® - Score Report - Synthetic Exam - Google Chrome")]]
        windows.close_report_window.side_effect = lambda *_: setattr(process.poll, "return_value", 0)
        await login.wait_for_handoff(process, windows, 1, MagicMock(), poll=0.001, stable_polls=2)
        windows.close_report_window.assert_called_once_with(12, 123)
        process.kill.assert_not_called()

    async def test_normal_login_timeout_never_closes_or_bypasses_authentication(self):
        process = MagicMock(pid=123)
        process.poll.return_value = None
        windows = MagicMock()
        windows.titles.return_value = [(11, "Sign in - Google Accounts - Google Chrome")]
        with self.assertRaisesRegex(ValueError, "left open"):
            await login.wait_for_handoff(process, windows, 0.01, MagicMock(), poll=0.001)
        windows.close_report_window.assert_not_called()
        process.terminate.assert_not_called()

    async def test_manual_normal_browser_close_hands_off_to_session_validation(self):
        process = MagicMock(pid=123)
        process.poll.side_effect = [None, 0]
        windows = MagicMock()
        windows.titles.return_value = [(11, "Sign in - Google Accounts - Google Chrome")]
        await login.wait_for_handoff(process, windows, 1, MagicMock(), poll=0.001)
        windows.close_report_window.assert_not_called()

    async def test_existing_browser_process_is_not_assumed_owned(self):
        process = MagicMock(pid=123)
        process.poll.return_value = 0
        windows = MagicMock()
        with self.assertRaisesRegex(ValueError, "already open"):
            await login.wait_for_handoff(process, windows, 1, MagicMock(), poll=0.001)
        windows.titles.assert_not_called()

    async def test_login_handoff_automatically_continues_after_user_signs_in(self):
        context, page = context_with("https://login.blueprintprep.com/u/login")
        watcher = run.LoginWatcher()

        async def sign_in():
            await asyncio.sleep(0.01)
            page.url = run.ANALYTICS
            watcher.ready.set()

        task = asyncio.create_task(sign_in())
        selected = await run.wait_for_login(context, watcher, timeout=1, poll=0.001)
        await task
        self.assertIs(selected, page)
        page.goto.assert_not_called()
        page.reload.assert_not_called()

    async def test_portal_url_alone_is_not_proof_of_login(self):
        context, _ = context_with(run.ANALYTICS)
        with self.assertRaisesRegex(ValueError, "Sign-in timed out"):
            await run.wait_for_login(context, run.LoginWatcher(), timeout=0.01, poll=0.001)

    async def test_auth_response_while_still_on_login_page_does_not_continue(self):
        context, _ = context_with("https://login.blueprintprep.com/u/login")
        watcher = run.LoginWatcher()
        watcher.ready.set()
        with self.assertRaisesRegex(ValueError, "Sign-in timed out"):
            await run.wait_for_login(context, watcher, timeout=0.01, poll=0.001)

    async def test_closing_browser_stops_wait_without_timeout(self):
        context, page = context_with(run.ANALYTICS)
        page.is_closed.return_value = True
        with self.assertRaisesRegex(ValueError, "Browser closed"):
            await run.wait_for_login(context, run.LoginWatcher(), timeout=100)

    async def test_successful_student_response_and_completed_ids_only(self):
        watcher = run.LoginWatcher()
        response = MagicMock()
        response.request = request()
        score, _ = raw_pair()
        response.body = AsyncMock(return_value=json.dumps(score).encode())
        await watcher.inspect(response)
        self.assertTrue(watcher.ready.is_set())
        self.assertEqual(watcher.activities, {"demo-activity"})
        self.assertFalse(hasattr(watcher, "body"))

    async def test_errors_null_students_and_mutations_are_not_login_evidence(self):
        for value in ({"data": {"student": None}}, {"data": {"student": {"id": "demo"}}, "errors": [{}]},
                      {"data": {"student": {}}}):
            watcher = run.LoginWatcher()
            response = MagicMock()
            response.request = request()
            response.body = AsyncMock(return_value=json.dumps(value).encode())
            await watcher.inspect(response)
            self.assertFalse(watcher.ready.is_set())
        response.request.post_data_json["query"] = "mutation { student { id } }"
        response.body = AsyncMock(return_value=b'{"data":{"student":{"id":"demo"}}}')
        await watcher.inspect(response)
        response.body.assert_not_called()

    async def test_capture_errors_and_session_expiry_stop_processing(self):
        context, _ = context_with(run.ANALYTICS)
        collector = report.ReportCollector("demo-activity")
        collector.errors.append("error")
        with self.assertRaisesRegex(ValueError, "validation"):
            await run.wait_for_report(context, collector, "score", 100)
        collector.errors.clear()
        context.pages[0].url = "https://login.blueprintprep.com/u/login"
        with self.assertRaisesRegex(ValueError, "expired"):
            await run.wait_for_report(context, collector, "score", 100)


class PipelineTests(unittest.TestCase):
    def test_multiple_reports_import_together_and_keep_retake_identity(self):
        first = bundle()
        second = copy.deepcopy(first)
        second["activity_id"] = "demo-retake"
        for kind in ("score", "identity"):
            activity(second, kind)["activityId"] = "demo-retake"
        with tempfile.TemporaryDirectory() as tmp:
            db, out, summary = [Path(tmp) / n for n in ("study.sqlite", "study_state.json", "summary.html")]
            result = run.process_bundles([first, second], db, out, summary)
            self.assertEqual(result["inserted"], 18)
            self.assertEqual(result["captured_attempts"], 16)
            state = report.read_json(out)
            self.assertEqual(len({r["question_id"] for r in state["records"]["attempts"]}), 8)
            self.assertEqual(len(state["sync_history"][0]["capture_metadata"]["reports"]), 2)
            self.assertEqual(run.process_bundles([first, second], db, out, summary)["status"], "already_imported")

    def test_invalid_second_report_preserves_db_and_both_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out, summary = [Path(tmp) / n for n in ("study.sqlite", "study_state.json", "summary.html")]
            run.process_bundles([bundle()], db, out, summary)
            before = (db.read_bytes(), out.read_bytes(), summary.read_bytes())
            broken = bundle()
            question(broken)["aggregate"]["correct"] = 10
            with self.assertRaises(ValueError):
                run.process_bundles([bundle(), broken], db, out, summary)
            self.assertEqual(before, (db.read_bytes(), out.read_bytes(), summary.read_bytes()))

    def test_duplicate_reports_and_account_mixing_rejected(self):
        with self.assertRaises(ValueError):
            run.combine([bundle(), bundle()])
        second = bundle()
        second["learner_id"] = "another-demo-learner"
        with self.assertRaises(ValueError):
            run.combine([bundle(), second])

    def test_summary_escapes_source_text_and_labels_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out, summary = [Path(tmp) / n for n in ("study.sqlite", "study_state.json", "summary.html")]
            b = bundle()
            for kind in ("score", "identity"):
                b[kind]["data"]["student"]["exams"]["items"][0]["title"] = "<script>alert('x')</script>"
            run.process_bundles([b], db, out, summary)
            text = summary.read_text(encoding="utf-8")
            self.assertNotIn("<script>", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertIn("Partial account coverage", text)
            self.assertIn("No data was uploaded", text)

    def test_capture_timestamp_provenance_rejected_if_inconsistent(self):
        data = run.combine([bundle()])
        data["capture_metadata"]["reports"][0]["response_captured_at"]["score"] = "2026-10-23T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "timestamp"):
            store.validate_snapshot(data)

    def test_existing_database_gains_provenance_column_without_losing_syncs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE syncs (hash TEXT PRIMARY KEY,captured_at TEXT,imported_at TEXT,coverage TEXT)")
            db.execute("INSERT INTO syncs VALUES ('demo',?,?,?)", (CAPTURED, CAPTURED, '{}'))
            db.commit()
            db.close()
            migrated = store.connect(path)
            try:
                self.assertEqual(migrated.execute("SELECT hash,capture_metadata FROM syncs").fetchall(), [("demo", None)])
            finally:
                migrated.close()

    def test_network_error_classification_omits_urls_and_tokens(self):
        text = run.browser_error(RuntimeError("Page.goto: net::ERR_NETWORK_ACCESS_DENIED https://example.invalid/?token=SECRET"))
        self.assertIn("execution environment", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("https", text)


class CredentialStartupTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(run, "PRIVATE", Path(self.tmp)))
        self.key = "sk-synthetic-startup-key-never-valid"
        self.credentials = self.stack.enter_context(patch.object(run.credentials, "get_api_key", return_value=self.key))
        self.forget = self.stack.enter_context(patch.object(run.credentials, "forget_api_key", return_value=True))
        self.capture = self.stack.enter_context(patch.object(run, "run", new_callable=AsyncMock))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def main(self, *args):
        with patch("sys.argv", ["blueprint_run.py", *args]):
            return run.main()

    def test_default_startup_resolves_credentials_before_capture(self):
        order = []
        self.credentials.side_effect = lambda **kwargs: order.append("credentials") or self.key

        async def capture(args, progress):
            order.append("capture")
            self.assertNotIn(self.key, repr(vars(args)))
            progress.update("complete", "Synthetic capture complete")

        self.capture.side_effect = capture
        self.assertEqual(self.main(), 0)
        self.assertEqual(order, ["credentials", "capture"])
        self.assertNotIn(self.key, self.output.getvalue())
        for path in Path(self.tmp).iterdir():
            self.assertNotIn(self.key, path.read_text())

    def test_local_only_skips_all_credential_access(self):
        self.assertEqual(self.main("--local-only"), 0)
        self.credentials.assert_not_called()
        self.forget.assert_not_called()
        self.capture.assert_awaited_once()

    def test_configure_replaces_key_and_exits_without_browser_or_report_plan(self):
        self.assertEqual(self.main("--configure-openai"), 0)
        self.credentials.assert_called_once_with(replace=True)
        self.capture.assert_not_called()
        self.assertEqual(list(Path(self.tmp).iterdir()), [])
        self.assertNotIn(self.key, self.output.getvalue())

    def test_forget_exits_without_prompt_or_browser(self):
        self.assertEqual(self.main("--forget-openai"), 0)
        self.forget.assert_called_once_with()
        self.credentials.assert_not_called()
        self.capture.assert_not_called()

    def test_setup_failure_stops_before_browser_and_returns_nonzero(self):
        self.credentials.side_effect = run.credentials.CredentialError("Safe setup failure")
        self.assertEqual(self.main(), 1)
        self.capture.assert_not_called()
        status = json.loads((Path(self.tmp) / "run_status.json").read_text())
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["message"], "Safe setup failure")

    def test_cancel_does_not_start_browser(self):
        self.credentials.side_effect = KeyboardInterrupt()
        self.assertEqual(self.main(), 1)
        self.capture.assert_not_called()

    def test_connection_options_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as error:
            self.main("--local-only", "--configure-openai")
        self.assertEqual(error.exception.code, 2)
        self.credentials.assert_not_called()
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
