"""Local companion setup, reports, imports, backup and transport-independent operations."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from companion import assessments, context, ingest, items, jobs, sync
from companion.service import Service
from companion.store import Store
from study_sync import atomic_json


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=os.environ.get("COMPANION_DATABASE", "private/companion.sqlite")
    )
    parser.add_argument(
        "--learner", default=os.environ.get("COMPANION_LEARNER", "local-learner")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--timezone", required=True)
    setup.add_argument("--preferences", type=Path)
    commands.add_parser("status")
    capture = commands.add_parser("import")
    capture.add_argument("file", type=Path)
    migrate = commands.add_parser("migrate-v1")
    migrate.add_argument("file", type=Path)
    report = commands.add_parser("report")
    report.add_argument("kind", choices=["daily", "weekly", "monthly"])
    report.add_argument("--as-of")
    report.add_argument(
        "--period-as-of",
        help="Anchor a historical period while using a later evidence cutoff",
    )
    report.add_argument("--timezone")
    report.add_argument("--out", type=Path)
    report.add_argument("--correction-reason")
    context_parser = commands.add_parser("upload-context")
    context_parser.add_argument("file", type=Path)
    context_parser.add_argument("--id", required=True)
    context_parser.add_argument("--title", required=True)
    context_parser.add_argument("--expected", type=int, default=0)
    context_parser.add_argument("--entries", type=Path)
    bank = commands.add_parser("install-item")
    bank.add_argument("file", type=Path)
    bank.add_argument("--review", required=True, type=Path)
    form = commands.add_parser("record-assessment")
    form.add_argument("file", type=Path)
    form.add_argument("--expected", type=int, default=0)
    call = commands.add_parser("call")
    call.add_argument(
        "operation",
        choices=[
            "offer",
            "start",
            "answer",
            "hint",
            "lesson_state",
            "reflection",
            "skip",
            "dispute",
            "due",
            "generate_job",
            "delivery",
        ],
    )
    call.add_argument(
        "file", type=Path, help="JSON arguments including a stable request_id"
    )
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
    capability = commands.add_parser("record-capability")
    capability.add_argument("name", choices=sorted(jobs.CAPABILITIES))
    capability.add_argument("status", choices=["verified", "failed", "unverified"])
    capability.add_argument("--evidence", required=True)
    commands.add_parser("sync")
    commands.add_parser(
        "retry-sync",
        help="Reset failed pending uploads after resolving the transport problem",
    )
    commands.add_parser(
        "prepare-due",
        help="Prepare due output on the backend without sending notifications",
    )
    generate = commands.add_parser(
        "generate-item",
        help="Explicit local billable authoring; requires configured models, prices and caps",
    )
    generate.add_argument("blueprint", type=Path)
    generate.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    store = None
    try:
        store = Store(args.db, args.learner)
        svc = Service(store)
        if args.command == "setup":
            values = (
                read_json(args.preferences)
                if args.preferences
                else {"timezone": args.timezone}
            )
            values["timezone"] = args.timezone
            old = store.get("preferences", "current")
            result = svc.preferences(str(uuid4()), values, old["version"] if old else 0)
        elif args.command == "status":
            result = svc.status()
        elif args.command == "import":
            data = read_json(args.file)
            result = ingest.ingest(store, data)
            if (
                data.get("format") == "blueprint_report_capture"
                and result["status"] != "quarantined"
            ):
                sync.enqueue(store, data)
        elif args.command == "migrate-v1":
            result = ingest.migrate_v1(store, args.file.resolve().as_posix())
        elif args.command == "report":
            ctx = svc.report_context(
                args.kind, args.as_of, args.timezone, args.period_as_of
            )
            result = svc.save_report(
                str(uuid4()),
                ctx["snapshot_id"],
                correction_reason=args.correction_reason,
            )
            if args.out:
                atomic_json(args.out, result)
            print(result["narrative"])
            return 0
        elif args.command == "upload-context":
            result = svc.upload_context(
                str(uuid4()),
                args.id,
                args.title,
                context.read_file(args.file),
                args.expected,
                read_json(args.entries) if args.entries else None,
            )
        elif args.command == "install-item":
            with store.transaction():
                result = items.install(
                    store, read_json(args.file), read_json(args.review)
                )
        elif args.command == "record-assessment":
            with store.transaction():
                result = assessments.record(store, read_json(args.file), args.expected)
        elif args.command == "call":
            result = getattr(svc, args.operation)(**read_json(args.file))
        elif args.command == "backup":
            result = store.backup(args.file)
        elif args.command == "restore":
            store.restore(read_json(args.file))
            result = {"status": "restored", "revision": store.revision()}
        elif args.command == "record-capability":
            with store.transaction():
                result = jobs.record_capability(
                    store, args.name, args.status, args.evidence
                )
        elif args.command == "sync":
            result = sync.synchronize(
                store,
                os.environ["COMPANION_SYNC_URL"],
                os.environ["COMPANION_COLLECTOR_TOKEN"],
            )
        elif args.command == "retry-sync":
            count = 0
            with store.transaction():
                for row in store.list("outbox"):
                    if row["state"] == "pending":
                        store.put(
                            "outbox",
                            row["id"],
                            {**row, "attempts": 0, "next_retry": None},
                        )
                        count += 1
            result = {"pending_uploads_reset": count}
        elif args.command == "prepare-due":
            due = svc.due(str(uuid4()))
            result = {
                "availability": due["availability"],
                "outputs": [
                    svc.generate_job(str(uuid4()), j["id"]) for j in due["jobs"]
                ],
            }
        elif args.command == "generate-item":
            from blueprint_credentials import get_api_key
            from companion.model_client import (
                ModelClient,
                ModelConfig,
                generate_reviewed_item,
            )

            config = ModelConfig(**read_json(args.config))
            result = generate_reviewed_item(
                ModelClient(store, config, get_api_key()), read_json(args.blueprint)
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        # Never print connection strings, tokens or response bodies on transport failures.
        print(
            "Companion operation failed: " + str(exc)
            if isinstance(exc, ValueError)
            else "Companion operation failed; check configuration and file access."
        )
        return 1
    except Exception:  # noqa: BLE001 -- CLI boundary must not print credential-bearing transport exceptions.
        print(
            "Companion operation failed; no successful save or sync is claimed. Check service availability and private configuration."
        )
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
