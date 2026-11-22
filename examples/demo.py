"""Import a synthetic study session, replay it, and summarize the saved evidence."""

import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from study_sync import connect, export_state, ingest, record_observation, timestamp


def main():
    fixtures = Path(__file__).resolve().parent
    snapshot = json.loads((fixtures / "normalized_snapshot.json").read_text(encoding="utf-8"))
    observation = json.loads((fixtures / "observation.json").read_text(encoding="utf-8"))
    as_of = timestamp("2026-10-22T20:00:00Z")

    with TemporaryDirectory() as directory, closing(connect(Path(directory) / "study.sqlite")) as db:
        first = ingest(db, snapshot, now=as_of)
        record_observation(db, observation)
        replay = ingest(db, snapshot, now=as_of)
        state = export_state(db, as_of)

    print("Blueprint Hope | synthetic study session")
    print(f"First import: {first['inserted']} records inserted")
    print(f"Replay: {replay['status']} ({replay['inserted']} records inserted)")
    print(f"Study notes retained: {len(state['local_observations'])}")
    print(f"Question history coverage: {state['latest_capture_coverage']['attempts']}")
    print("\nTopic                       Correct / answered (7 days)")
    for summary in state["topic_summaries"]:
        week = summary["windows"]["7d"]
        print(f"{summary['topic']:<28}{week['correct']} / {week['answered']}")


if __name__ == "__main__":
    main()
