"""Synthetic score, identity and request fixtures for the completed-report adapter."""

import copy
from unittest.mock import MagicMock

import blueprint_report as report

CAPTURED = "2026-10-22T19:00:00Z"
START = "2026-10-22T16:00:00.123456789Z"
LAST = "2026-10-22T18:00:00Z"


def aggregate(total=1, correct=1, answered=None):
    answered = total if answered is None else answered
    return dict(correct=correct, incorrect=answered-correct, total=total, answered=answered,
                incomplete=total-answered, seen=total, startTime=START, lastSeen=LAST,
                endTime=None, isComplete=True, correctToIncorrect=0,
                incorrectToIncorrect=0, incorrectToCorrect=0)


def raw_pair():
    sections = []
    for index, name in enumerate(report.SECTIONS):
        questions = []
        for n in range(2):
            questions.append({"flagged": False, "answer": ["demo-answer"], "previousAnswer": [],
                              "timeSpent": 10+n,
                              "aggregate": aggregate(correct=n),
                              "question": {"metadata": [
                                  {"id": "demo-topic", "title": "Synthetic topic", "displayName": "Topic"},
                                  {"id": "demo-skill", "title": "Synthetic reasoning", "displayName": "Reasoning Skills"}]}})
        sections.append({"score": 120+index, "section": {"shortTitle": name},
                         "aggregate": aggregate(2, 1),
                         "questionGroups": [{"questionGroup": {"discrete": False}, "questions": questions}]})
    exam = {"title": "Synthetic completed exam", "mode": "STANDARD", "isAuthorized": True,
            "activities": [{"activityId": "demo-activity", "score": 486,
                            "aggregate": aggregate(8, 4), "sections": sections},
                           {"activityId": None, "aggregate": None, "sections": []}]}
    score = {"data": {"student": {"exams": {"total": 1, "items": [exam]}}}}
    identity = copy.deepcopy(score)
    ie = identity["data"]["student"]["exams"]["items"][0]
    ie["id"] = "demo-exam"
    ie["description"] = "UNWANTED EXAM DESCRIPTION"
    del ie["isAuthorized"]
    a = ie["activities"][0]
    del a["score"]
    for ix, sec in enumerate(a["sections"]):
        sec["section"]["id"] = f"demo-section-{ix}"
        del sec["score"]
        for grp in sec["questionGroups"]:
            grp["questionGroup"].update(id=f"demo-group-{ix}", description="UNWANTED PASSAGE")
            for n, q in enumerate(grp["questions"]):
                q["question"] = {"id": f"demo-question-{ix}-{n}"}
                del q["timeSpent"]
                q["aggregate"] = report.pick(q["aggregate"], ("total", "seen", "lastSeen"))
    return score, identity


def bundle():
    score, identity = raw_pair()
    return report.make_bundle(score, identity, "DEMO_ONLY", "demo-activity", CAPTURED)


def activity(value, kind="score"):
    return value[kind]["data"]["student"]["exams"]["items"][0]["activities"][0]


def question(value, kind="score"):
    return activity(value, kind)["sections"][0]["questionGroups"][0]["questions"][0]


QUERY = "query($activityId: ID) { student { exams(activityId:$activityId) { total items { title } } } }"


def request():
    req = MagicMock()
    req.url, req.method = report.API, "POST"
    req.post_data_json = {"query": QUERY, "variables": {"activityId": "demo-activity"}}
    return req
