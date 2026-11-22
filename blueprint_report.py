"""Capture one completed Blueprint MCAT report, normalize it, and update local study context.

Only the observed score-report and exam-review response shapes are supported.
Account enumeration, Qbank capture, and delivery to ChatGPT are separate work.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import study_sync as store

PRIVATE = Path(__file__).resolve().parent / "private"
API = "https://api-composite.blueprintprep.com/query"
SECTIONS = {"Chem/Phys": "CP", "CARS": "CARS", "Bio/Biochem": "BB", "Psych/Soc": "PS"}
COUNTS = ("correct", "incorrect", "total", "answered", "incomplete")
AGGREGATE = COUNTS + ("seen", "lastSeen", "startTime", "endTime", "isComplete",
                      "correctToIncorrect", "incorrectToIncorrect", "incorrectToCorrect")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pick(obj, keys):
    require(isinstance(obj, dict), "Expected a report object")
    return {key: obj[key] for key in keys if key in obj}


def select_activity(response, activity_id):
    require(isinstance(response, dict) and not response.get("errors"), "GraphQL response contains errors")
    try:
        exams = response["data"]["student"]["exams"]
        require(isinstance(exams["items"], list), "Missing exam list")
        matches = [(exam, activity) for exam in exams["items"] for activity in exam["activities"]
                   if activity.get("activityId") == activity_id]
        require(len(matches) == 1, "Expected exactly one matching activity; no results are imported")
        return matches[0]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Missing or changed report structure") from exc


def project(response, activity_id, kind):
    """Keep performance/identity fields only; discard passages, descriptions and other data."""
    require(kind in {"score", "identity"}, "Unknown report kind")
    exam, activity = select_activity(response, activity_id)
    output = pick(exam, ("id", "title", "mode", "isAuthorized"))
    result = pick(activity, ("activityId", "score", "percentile"))
    result["aggregate"] = pick(activity["aggregate"], AGGREGATE)
    result["sections"] = []
    for section in activity["sections"]:
        sec = pick(section, ("score", "percentile"))
        sec["section"] = pick(section["section"], ("id", "shortTitle"))
        sec["aggregate"] = pick(section["aggregate"], AGGREGATE)
        sec["questionGroups"] = []
        for group in section["questionGroups"]:
            grp = {"questionGroup": pick(group["questionGroup"], ("id", "discrete")), "questions": []}
            for question in group["questions"]:
                item = pick(question, ("flagged", "answer", "previousAnswer", "timeSpent"))
                item["aggregate"] = pick(question["aggregate"], AGGREGATE)
                item["question"] = pick(question["question"], ("id",))
                if kind == "score":
                    item["question"]["metadata"] = [pick(tag, ("id", "title", "displayName"))
                                                     for tag in question["question"]["metadata"]]
                grp["questions"].append(item)
            sec["questionGroups"].append(grp)
        result["sections"].append(sec)
    output["activities"] = [result]
    return {"data": {"student": {"exams": {"total": 1, "items": [output]}}}}


def make_bundle(score, identity, learner_id, activity_id, captured_at, response_times=None):
    store.nonempty(learner_id, "learner_id")
    store.nonempty(activity_id, "activity_id")
    store.timestamp(captured_at)
    return {"format": "blueprint_report_capture", "schema_version": 1,
            "learner_id": learner_id, "activity_id": activity_id, "captured_at": captured_at,
            "response_captured_at": response_times or {"score": None, "identity": None},
            "score": project(score, activity_id, "score"),
            "identity": project(identity, activity_id, "identity")}


def shared_matches(left, right, keys, label):
    for key in keys:
        require(key in left and key in right and left[key] == right[key], f"{label}: response mismatch ({key})")


def source_times(aggregate):
    # Preserve exact source strings in the capture; SQLite normalizes to microseconds.
    start, last = aggregate.get("startTime"), aggregate.get("lastSeen")
    store.timestamp(start)
    store.timestamp(last)
    require(store.timestamp(start) <= store.timestamp(last), "Invalid source time order")
    return {"occurred_at": start, "updated_at": last}


def integer(value, label):
    require(type(value) is int and value >= 0, f"{label}: expected a nonnegative integer")
    return value


def reconcile(aggregate, questions, label):
    require(aggregate.get("isComplete") is True, f"{label}: only completed reports are supported")
    for key in COUNTS:
        actual = sum(integer(q["aggregate"].get(key), key) for q in questions)
        require(integer(aggregate.get(key), key) == actual, f"{label}: {key} does not reconcile")


def answer_change(aggregate):
    values = [integer(aggregate.get(k), k) for k in
              ("correctToIncorrect", "incorrectToCorrect", "incorrectToIncorrect")]
    require(sum(values) <= 1, "Unsupported multiple answer-change history")
    return next((label for count, label in zip(values, ("right_to_wrong", "wrong_to_right", "wrong_to_wrong"))
                 if count), "none")


def normalize(bundle):
    """Fail closed on incomplete, mismatched or unsupported source data."""
    try:
        require(bundle["format"] == "blueprint_report_capture" and bundle["schema_version"] == 1,
                "Unsupported report capture format")
        activity_id = bundle["activity_id"]
        score_exam, score = select_activity(bundle["score"], activity_id)
        identity_exam, identity = select_activity(bundle["identity"], activity_id)
        store.nonempty(identity_exam["id"], "exam id")
        require(score_exam.get("isAuthorized") is True, "Report access is not authorized")
        shared_matches(score_exam, identity_exam, ("title", "mode"), "exam")
        shared_matches(score["aggregate"], identity["aggregate"],
                       ("startTime", "lastSeen", "endTime", "isComplete"), "activity")
        score_sections = {s["section"]["shortTitle"]: s for s in score["sections"]}
        id_sections = {s["section"]["shortTitle"]: s for s in identity["sections"]}
        require(len(score["sections"]) == len(identity["sections"]) == 4 and
                set(score_sections) == set(id_sections) == set(SECTIONS), "Expected four unique MCAT sections")
        assessment_id = "blueprint:activity:" + activity_id
        attempts, questions, seen_ids, scores = [], [], set(), {"total": score["score"]}
        for source_name, section_name in SECTIONS.items():
            ss, si = score_sections[source_name], id_sections[source_name]
            store.nonempty(si["section"]["id"], "section id")
            shared_matches(ss["aggregate"], si["aggregate"],
                           ("seen", "lastSeen", "startTime", "endTime", "answered", "isComplete"), "section")
            scores[section_name] = ss["score"]
            require(len(ss["questionGroups"]) == len(si["questionGroups"]), "Question group count mismatch")
            section_questions = []
            for gs, gi in zip(ss["questionGroups"], si["questionGroups"]):
                store.nonempty(gi["questionGroup"]["id"], "question group id")
                shared_matches(gs["questionGroup"], gi["questionGroup"], ("discrete",), "question group")
                require(len(gs["questions"]) == len(gi["questions"]), "Question count mismatch")
                for qs, qi in zip(gs["questions"], gi["questions"]):
                    shared_matches(qs, qi, ("answer", "previousAnswer", "flagged"), "question")
                    shared_matches(qs["aggregate"], qi["aggregate"], ("total", "seen", "lastSeen"), "question")
                    question_id = qi["question"]["id"]
                    store.nonempty(question_id, "question id")
                    require(question_id not in seen_ids, "Duplicate question ID in one activity")
                    seen_ids.add(question_id)
                    agg = qs["aggregate"]
                    require(agg.get("isComplete") is True and agg.get("total") == 1, "Unsupported question result")
                    correct, incorrect, answered, incomplete = [integer(agg.get(k), k) for k in
                                                               ("correct", "incorrect", "answered", "incomplete")]
                    require(correct + incorrect == answered and answered + incomplete == 1,
                            "Question result counts are inconsistent")
                    tags = qs["question"]["metadata"]
                    topics = sorted({t["title"] for t in tags if t["displayName"] == "Topic"})
                    skills = sorted({t["title"] for t in tags if t["displayName"] == "Reasoning Skills"})
                    require(topics and len(skills) <= 1, "Missing topics or unsupported multiple reasoning skills")
                    attempts.append({"id": "blueprint:attempt:" + store.digest([activity_id, question_id]),
                                     "question_id": question_id, "assessment_id": assessment_id,
                                     "section": section_name, "topic": " | ".join(topics), "topics": topics,
                                     "skill": skills[0] if skills else None,
                                     "correct": bool(correct) if answered else None,
                                     "seconds": qs["timeSpent"], "bookmarked": qs["flagged"],
                                     "answer_change": answer_change(agg), "mode": score_exam["mode"],
                                     **source_times(agg)})
                    section_questions.append(qs)
            reconcile(ss["aggregate"], section_questions, section_name)
            questions.extend(section_questions)
        require(attempts, "The report has no question attempts")
        reconcile(score["aggregate"], questions, "activity")
        snapshot = {"schema_version": 1, "source": "blueprint_mcat", "learner_id": bundle["learner_id"],
                    "captured_at": bundle["captured_at"],
                    "capture_metadata": {"adapter": "blueprint_report_v1", "scope": "one_completed_activity",
                                         "response_captured_at": bundle["response_captured_at"]},
                    "coverage": {"attempts": "partial", "assessments": "partial", "activities": "unavailable"},
                    "attempts": sorted(attempts, key=lambda r: r["id"]),
                    "assessments": [{"id": assessment_id, "name": score_exam["title"], "scores": scores,
                                     "mode": score_exam["mode"], **source_times(score["aggregate"])}],
                    "activities": []}
        return store.validate_snapshot(snapshot)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Missing or changed Blueprint report fields; previous export is preserved") from exc


def sync_bundle(bundle, db_path, out_path):
    snapshot = normalize(bundle)
    db = store.connect(db_path)
    try:
        result = store.ingest(db, snapshot)
        exported = store.export_state(db)
        store.atomic_json(out_path, exported)
    finally:
        db.close()
    return {**result, "report_attempts": len(snapshot["attempts"]), "coverage": "partial"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def request_matches(request, activity_id):
    """Narrowly observe UI-issued read queries; never record headers or login responses."""
    if request.url != API or request.method != "POST":
        return False
    try:
        body = request.post_data_json
        query = body["query"]
        return (body.get("variables") == {"activityId": activity_id}
                and isinstance(query, str) and re.match(r"^\s*query\b", query) is not None
                and re.search(r"\b(mutation|subscription)\b", query) is None
                and re.search(r"student\s*\{\s*exams\s*\(\s*activityId\s*:\s*\$activityId\s*\)", query) is not None)
    except (ValueError, KeyError, TypeError):
        return False


class ReportCollector:
    def __init__(self, activity_id):
        self.activity_id = activity_id
        self.responses, self.times, self.errors = {}, {}, []
        self.events = {kind: asyncio.Event() for kind in ("score", "identity")}
        self.pending = set()

    def on_response(self, response):
        if request_matches(response.request, self.activity_id):
            task = asyncio.create_task(self.inspect(response))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def inspect(self, response):
        try:
            require(response.status == 200, "Report request failed; check sign-in")
            body = await response.body()
            require(len(body) <= 5_000_000, "Report response exceeds supported size")
            value = json.loads(body)
            exam, activity = select_activity(value, self.activity_id)
            if "score" in activity:
                kind = "score"
            elif "id" in exam:
                kind = "identity"
            else:
                return
            projected = project(value, self.activity_id, kind)
            if kind in self.responses:
                require(projected == self.responses[kind], "Report changed during capture; retry")
            else:
                self.responses[kind] = projected
                self.times[kind] = store.iso(store.utcnow())
                self.events[kind].set()
        except Exception:
            # No raw exceptions, bodies, headers, URLs, or credential values in logs.
            self.errors.append("A matching report response failed validation; retry capture")

    async def finish(self):
        if self.pending:
            _, pending = await asyncio.wait(list(self.pending), timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                self.errors.append("Report response capture timed out")
        require(not self.errors, "Report capture failed; previous export is preserved")


async def capture_report(args):
    from playwright.async_api import async_playwright

    if args.from_capture:
        previous = read_json(args.from_capture)
        normalize(previous)
        require(not args.learner_id and not args.activity_id,
                "Use from-capture or learner-id/activity-id, not both")
        args.learner_id, args.activity_id = previous["learner_id"], previous["activity_id"]
    store.nonempty(args.learner_id, "learner_id")
    store.nonempty(args.activity_id, "activity_id")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", args.activity_id) is not None, "Invalid activity ID")
    collector = ReportCollector(args.activity_id)
    async with async_playwright() as pw:
        options = {"headless": args.headless, "accept_downloads": False}
        if args.channel != "chromium":
            options["channel"] = args.channel
        context = await pw.chromium.launch_persistent_context(str(PRIVATE / "blueprint_profile"), **options)
        context.on("response", collector.on_response)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            routes = (("score", f"https://mcat.blueprintprep.com/scorereport/{args.activity_id}"),
                      ("identity", f"https://mcat.blueprintprep.com/exam_explanation/{args.activity_id}/0/0?iframe=true"))
            for kind, url in routes:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await asyncio.wait_for(collector.events[kind].wait(), timeout=args.timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise ValueError("Report unavailable or sign-in required. Use blueprint_browser.py capture to sign in normally, then retry.") from exc
            context.remove_listener("response", collector.on_response)
            await collector.finish()
        finally:
            context.remove_listener("response", collector.on_response)
            await context.close()
            if collector.pending:
                for task in collector.pending:
                    task.cancel()
                await asyncio.gather(*list(collector.pending), return_exceptions=True)
    bundle = make_bundle(collector.responses["score"], collector.responses["identity"], args.learner_id,
                         args.activity_id, max(collector.times.values()), collector.times)
    normalize(bundle)
    path = PRIVATE / "report_captures" / (re.sub(r"\D", "", bundle["captured_at"]) + ".json")
    store.atomic_json(path, bundle)
    return sync_bundle(bundle, args.db, args.out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack", help="Package two manually saved response JSON files")
    for name in ("score", "identity", "learner-id", "activity-id", "captured-at", "out"):
        pack.add_argument("--" + name, required=True)
    pack.add_argument("--score-captured-at")
    pack.add_argument("--identity-captured-at")
    sync = commands.add_parser("sync", help="Import a saved capture and export study context")
    sync.add_argument("--capture", required=True)
    capture = commands.add_parser("capture", help="Capture one report using the dedicated browser profile and sync it")
    capture.add_argument("--learner-id")
    capture.add_argument("--activity-id")
    capture.add_argument("--from-capture", type=Path, help="Reuse learner/activity IDs from a validated saved capture")
    capture.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    capture.add_argument("--headless", action="store_true")
    capture.add_argument("--timeout-seconds", type=int, default=60)
    for command in (sync, capture):
        command.add_argument("--db", type=Path, default=PRIVATE / "study.sqlite")
        command.add_argument("--out", type=Path, default=PRIVATE / "study_state.json")
    args = parser.parse_args()
    try:
        if args.command == "pack":
            bundle = make_bundle(read_json(args.score), read_json(args.identity), args.learner_id,
                                 args.activity_id, args.captured_at,
                                 {"score": args.score_captured_at, "identity": args.identity_captured_at})
            normalize(bundle)
            store.atomic_json(args.out, bundle)
            print("Validated report capture saved locally. Coverage: partial.")
        elif args.command == "sync":
            print(store.encode(sync_bundle(read_json(args.capture), args.db, args.out)))
        else:
            require(1 <= args.timeout_seconds <= 300, "timeout-seconds must be 1 through 300")
            print(store.encode(asyncio.run(capture_report(args))))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "Local file operation failed; check file paths and permissions")
        return 1
    except ImportError:
        print("Install requirements-browser.txt before browser capture")
        return 1
    except KeyboardInterrupt:
        print("Capture stopped. Previous context remains available.")
        return 1
    except Exception:
        print("Browser capture failed. Check the dedicated profile and sign-in, then retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
