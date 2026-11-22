"""Open Blueprint, wait for normal sign-in, capture supported reports, and show local results."""
from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import blueprint_report as report
import blueprint_credentials as credentials
from blueprint_login import normal_login
import study_sync as store
from blueprint_browser import atomic_text

PRIVATE = report.PRIVATE
ANALYTICS = "https://mcat.blueprintprep.com/analytics"
PORTAL_LAUNCH = "https://blueprintprep.com/lms/access/redirect/test/mcat/"


class Progress:
    def __init__(self, path):
        self.path = path
        self.started = store.iso(store.utcnow())

    def update(self, phase, message, **details):
        print(message, flush=True)
        store.atomic_json(self.path, {"phase": phase, "message": message, "started_at": self.started,
                                     "updated_at": store.iso(store.utcnow()), **details})


class LoginWatcher:
    """Observe a successful student query, without reading credentials or saving account bodies."""
    def __init__(self):
        self.ready = asyncio.Event()
        self.activities = set()
        self.pending = set()

    def on_response(self, response):
        request = response.request
        if request.url == report.API and request.method == "POST" and response.status == 200:
            task = asyncio.create_task(self.inspect(response))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def inspect(self, response):
        try:
            query = response.request.post_data_json.get("query", "")
            if not re.match(r"^\s*query\b", query) or re.search(r"\b(mutation|subscription)\b", query):
                return
            body = await response.body()
            if len(body) > 5_000_000:
                return
            import json
            value = json.loads(body)
            student = value.get("data", {}).get("student")
            if value.get("errors") or not isinstance(student, dict) or not student:
                return
            self.ready.set()
            # Use only IDs and completion flags returned by the app's own queries.
            for exam in student.get("exams", {}).get("items", []):
                for activity in exam.get("activities", []):
                    aggregate = activity.get("aggregate") or {}
                    identifier = activity.get("activityId")
                    if aggregate.get("isComplete") is True and valid_id(identifier):
                        self.activities.add(identifier)
        except Exception:
            # Other API responses are not login evidence. Never print their bodies.
            return

    async def close(self):
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def portal_page(context):
    for page in context.pages:
        if not page.is_closed() and urlsplit(page.url).hostname == "mcat.blueprintprep.com":
            return page
    return None


async def wait_for_login(context, watcher, timeout, poll=0.5):
    """Poll only app readiness. Do not reload or interrupt password/MFA entry."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not any(not page.is_closed() for page in context.pages):
            raise ValueError("Browser closed before sign-in finished. Run the script again when ready.")
        page = portal_page(context)
        if page is not None and watcher.ready.is_set():
            return page
        await asyncio.sleep(poll)
    raise ValueError("Sign-in timed out. Run again and finish normal sign-in, including MFA if requested.")


async def wait_for_report(context, collector, kind, timeout, poll=0.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if collector.errors:
            raise ValueError("A report response failed validation. The previous study export was preserved.")
        if collector.events[kind].is_set():
            return
        if not any(not p.is_closed() for p in context.pages):
            raise ValueError("Browser closed during capture. Run again to retry.")
        if any(urlsplit(p.url).hostname == "login.blueprintprep.com" for p in context.pages if not p.is_closed()):
            raise ValueError("Sign-in expired during capture. Run again to sign in and resume automatically.")
        await asyncio.sleep(poll)
    raise ValueError("The expected report response did not arrive. The report may be unavailable or its format changed.")


def load_plan(path, seed=None):
    if seed:
        capture = report.read_json(seed)
        report.normalize(capture)
        plan = {"schema_version": 1, "learner_id": capture["learner_id"], "activity_ids": [capture["activity_id"]]}
    else:
        report.require(path.exists(), "No report plan yet. Run with --from-capture PATH to a validated report capture first.")
        plan = report.read_json(path)
    report.require(isinstance(plan, dict) and set(plan) == {"schema_version", "learner_id", "activity_ids"}
                   and plan["schema_version"] == 1, "Invalid report plan")
    store.nonempty(plan["learner_id"], "learner_id")
    ids = plan["activity_ids"]
    report.require(isinstance(ids, list) and ids and all(valid_id(i) for i in ids)
                   and len(set(ids)) == len(ids), "The plan requires unique observed activity IDs")
    return plan


def combine(bundles):
    """Validate all reports before a single transactional import."""
    report.require(bool(bundles), "No completed reports captured")
    snapshots = [report.normalize(bundle) for bundle in bundles]
    owners = {s["learner_id"] for s in snapshots}
    report.require(len(owners) == 1, "Cannot combine reports from different learners")
    result = {"schema_version": 1, "source": "blueprint_mcat", "learner_id": snapshots[0]["learner_id"],
              "captured_at": max((s["captured_at"] for s in snapshots), key=store.timestamp),
              "coverage": {"attempts": "partial", "assessments": "partial", "activities": "unavailable"},
              "attempts": [], "assessments": [], "activities": [],
              "capture_metadata": {"adapter": "blueprint_report_v1", "scope": "selected_completed_activities",
                                   "reports": []}}
    for bundle, snapshot in zip(bundles, snapshots):
        for kind in ("attempts", "assessments"):
            result[kind].extend(snapshot[kind])
        result["capture_metadata"]["reports"].append({"activity_id": bundle["activity_id"],
            "captured_at": bundle["captured_at"], "response_captured_at": bundle["response_captured_at"]})
    for kind in ("attempts", "assessments"):
        result[kind].sort(key=lambda row: row["id"])
    return store.validate_snapshot(result)


def render_summary(state):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = state["records"]["attempts"]
    section_rows = []
    for section in ("CP", "CARS", "BB", "PS"):
        items = [r for r in rows if r["section"] == section]
        stats = store.metrics(items)
        accuracy = f"{stats['accuracy']:.0%}" if stats["accuracy"] is not None else "—"
        section_rows.append(f"<tr><th>{section}</th><td>{stats['attempts']}</td>"
                            f"<td>{stats['correct']} / {stats['answered']}</td><td>{accuracy}</td></tr>")
    scores = "".join(f"<li>{esc(a['name'])}: {esc(a['scores'].get('total', '—'))}</li>"
                     for a in state["records"]["assessments"])
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Blueprint study summary</title><style>
body{{font:17px/1.6 system-ui,sans-serif;background:#f4f7fa;color:#142f40;margin:0}}
main{{max-width:850px;margin:3rem auto;padding:2rem;background:white;border-radius:16px}}
h1{{margin-top:0}} .notice{{padding:1rem;background:#fff4d9;border-radius:8px}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:.7rem;border-bottom:1px solid #dbe4eb}}
small{{color:#4c6170}} a{{color:#005da8}} @media(max-width:600px){{main{{margin:0;border-radius:0;padding:1rem}}}}
</style><main><h1>Your Blueprint study context</h1>
<p>{len(rows)} question attempts · {len(state['records']['assessments'])} {'assessment' if len(state['records']['assessments']) == 1 else 'assessments'} · saved locally</p>
<p class="notice"><strong>Partial account coverage.</strong> This includes supported completed reports captured so far.
Qbank, flashcards, progress, and unvisited reports may be missing. No data was uploaded.</p>
<h2>Captured performance</h2><table><thead><tr><th>Section</th><th>Attempts</th><th>Correct / answered</th><th>Accuracy</th></tr></thead>
<tbody>{''.join(section_rows)}</tbody></table><p>Retakes count separately. These totals describe the saved records, not mastery.</p>
<h2>Assessment scores</h2><ul>{scores}</ul>
<h2>Study context is ready</h2><p><a href="study_state.json">Open the structured study context</a>.</p>
<p>Local tutor observations retained: {len(state['local_observations'])}.</p>
<p><small>Capture completed: {esc(state['source_captured_at'])}<br>Summary generated: {esc(state['generated_at'])}</small></p>
</main></html>"""


def process_bundles(bundles, db_path, out_path, summary_path):
    snapshot = combine(bundles)
    db = store.connect(db_path)
    try:
        result = store.ingest(db, snapshot)
        state = store.export_state(db)
        rendered = render_summary(state)
        store.atomic_json(out_path, state)
        atomic_text(summary_path, rendered)
    finally:
        db.close()
    # Additive companion ledger/outbox; no network or model calls in capture.
    from companion.store import Store as CompanionStore
    from companion.ingest import ingest as ingest_companion
    from companion.sync import enqueue
    companion = CompanionStore(Path(db_path).parent / "companion.sqlite",
                               learner=os.environ.get("COMPANION_LEARNER", "local-learner"))
    try:
        companion_results = []
        for bundle in bundles:
            companion_results.append(ingest_companion(companion, bundle))
            enqueue(companion, bundle)
    finally:
        companion.close()
    return {**result, "captured_reports": len(bundles), "captured_attempts": len(snapshot["attempts"]),
            "stored_attempts": len(state["records"]["attempts"]), "coverage": "partial",
            "companion": {"captures": len(companion_results),
                          "quarantined": sum(r["status"] == "quarantined" for r in companion_results),
                          "sync": "queued_locally"}}


def browser_error(exc):
    # Classify useful known failures without printing browser logs, account URLs or auth state.
    message = str(exc)
    if "ERR_NETWORK_ACCESS_DENIED" in message:
        return "Browser network access was blocked by the execution environment. Run the launcher in a normal local session."
    if "ERR_NAME_NOT_RESOLVED" in message or "ERR_INTERNET_DISCONNECTED" in message:
        return "Blueprint could not be reached. Check the network connection and retry."
    if "Executable doesn't exist" in message:
        return "Chrome was not found. Install Chrome or use --channel msedge."
    if "ProcessSingleton" in message or "user data directory is already in use" in message:
        return "The dedicated browser profile is already open. Close its other run and retry."
    return "Browser navigation or capture failed. Close this run, check your connection, and retry."


async def run(args, progress):
    from playwright.async_api import async_playwright

    plan = load_plan(args.plan, args.from_capture)
    watcher = LoginWatcher()
    bundles = []
    await normal_login(PRIVATE / "blueprint_profile", args.channel,
                       args.login_timeout, progress)
    progress.update("opening_browser", "Opening Blueprint in the dedicated Chrome profile...")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(PRIVATE / "blueprint_profile"),
            channel=args.channel, headless=False, accept_downloads=False)
        context.on("response", watcher.on_response)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            progress.update("verifying_session", "Verifying the saved Blueprint session...")
            await page.goto(PORTAL_LAUNCH, wait_until="domcontentloaded", timeout=45000)
            try:
                page = await wait_for_login(context, watcher, args.report_timeout)
            except ValueError as exc:
                raise ValueError("The saved Blueprint session was not usable. Run again and finish sign-in in the normal browser; do not sign into Google in the capture browser.") from exc
            progress.update("discovering", "Sign-in detected. Finding supported completed reports...")
            await page.goto(ANALYTICS, wait_until="domcontentloaded", timeout=45000)
            identifiers = sorted(set(plan["activity_ids"]) | watcher.activities)
            for index, activity_id in enumerate(identifiers, 1):
                progress.update("capturing", f"Capturing report {index} of {len(identifiers)}...",
                                report_index=index, report_total=len(identifiers))
                collector = report.ReportCollector(activity_id)
                context.on("response", collector.on_response)
                try:
                    for kind, url in (("score", f"https://mcat.blueprintprep.com/scorereport/{activity_id}"),
                                      ("identity", f"https://mcat.blueprintprep.com/exam_explanation/{activity_id}/0/0?iframe=true")):
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        await wait_for_report(context, collector, kind, args.report_timeout)
                    await collector.finish()
                    bundle = report.make_bundle(collector.responses["score"], collector.responses["identity"],
                        plan["learner_id"], activity_id, max(collector.times.values()), collector.times)
                    report.normalize(bundle)
                    bundles.append(bundle)
                finally:
                    context.remove_listener("response", collector.on_response)
                    tasks = list(collector.pending)
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            context.remove_listener("response", watcher.on_response)
            await watcher.close()
            await context.close()
    progress.update("processing", "Validating reports and updating your local study context...")
    for bundle in bundles:
        stamp = re.sub(r"\D", "", bundle["captured_at"])
        store.atomic_json(PRIVATE / "report_captures" / f"{stamp}-{store.digest(bundle)[:12]}.json", bundle)
    result = process_bundles(bundles, PRIVATE / "study.sqlite", PRIVATE / "study_state.json", PRIVATE / "study_summary.html")
    store.atomic_json(args.plan, {**plan, "activity_ids": identifiers})
    progress.update("complete", "Done. Your local study summary is ready. Account coverage is partial; no upload was performed.", **result)
    if not args.no_open:
        webbrowser.open((PRIVATE / "study_summary.html").as_uri())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=PRIVATE / "report_plan.json")
    parser.add_argument("--from-capture", type=Path)
    parser.add_argument("--channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--login-timeout", type=int, default=900, help="Seconds allowed for normal sign-in (default 15 minutes)")
    parser.add_argument("--report-timeout", type=int, default=60)
    parser.add_argument("--no-open", action="store_true", help="Save the results without opening the local summary")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--local-only", action="store_true", help="Capture reports without OpenAI API-key setup")
    connection.add_argument("--configure-openai", action="store_true",
                            help="Prompt to save or replace the local OpenAI API key, then exit")
    connection.add_argument("--forget-openai", action="store_true",
                            help="Remove this tool's saved OpenAI API key, then exit")
    args = parser.parse_args()
    if not 1 <= args.login_timeout <= 3600 or not 1 <= args.report_timeout <= 300:
        parser.error("login-timeout must be 1..3600; report-timeout must be 1..300")
    progress = Progress(PRIVATE / "run_status.json")
    try:
        if args.forget_openai:
            removed = credentials.forget_api_key()
            print("Saved OpenAI API key removed." if removed else "No saved OpenAI API key was found.")
            print("OPENAI_API_KEY, if set, is unchanged. This does not revoke the key at OpenAI.")
            return 0
        if not args.local_only:
            # Configure before opening browsers. Never put the secret in args, progress,
            # files, or the subprocess environment. API delivery is not implemented yet.
            credentials.get_api_key(replace=args.configure_openai)
            if args.configure_openai:
                print("OpenAI API-key setup complete. No connection test or upload was performed.")
                return 0
        asyncio.run(run(args, progress))
        return 0
    except KeyboardInterrupt:
        progress.update("cancelled", "Stopped. Previously saved study context remains available.")
    except ImportError:
        progress.update("failed", "Install requirements-browser.txt with the same Python interpreter, then run again.")
    except ValueError as exc:
        progress.update("failed", str(exc))
    except OSError:
        progress.update("failed", "A local file operation failed. Check file permissions and retry.")
    except Exception as exc:
        progress.update("failed", browser_error(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
