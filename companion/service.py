"""Narrow application operations shared by CLI, HTTP and MCP.

Authentication belongs to the transport. A Service instance is bound to the
server-configured learner, never a caller-supplied learner ID.
"""

from companion import context as uploads
from companion import items, jobs, lessons, reports
from companion.models import Preferences
from companion.store import Conflict


class Service:
    def __init__(self, store):
        self.store = store

    def status(self):
        with self.store.transaction():
            return {
                "schema_version": 2,
                "freshness": reports.freshness(self.store, self.store.now()),
                "preferences": self.store.get("preferences", "current"),
                "active_session": next(
                    (
                        {
                            "session_id": s["id"],
                            "version": s["version"],
                            "state": s["state"],
                        }
                        for s in self.store.list("session")
                        if s["state"] in {"started", "paused"}
                    ),
                    None,
                ),
                "capabilities": self.store.list("capability"),
                "input_revision": self.store.revision(),
                "bank": {
                    "accepted": sum(
                        i["status"] == "accepted" for i in self.store.list("item")
                    ),
                    "quarantined": sum(
                        i["status"] == "quarantined" for i in self.store.list("item")
                    ),
                },
                "coverage": "partial",
                "scheduled_delivery_verified": jobs.ready(self.store),
                "conflicting_captures": sum(
                    c["status"] == "quarantined" for c in self.store.list("capture")
                ),
                "sync_health": {
                    "last_download": self.store.get("sync_cursor", "hosted"),
                    "pending_uploads": sum(
                        r["state"] == "pending" for r in self.store.list("outbox")
                    ),
                    "exhausted_retries": sum(
                        r["state"] == "pending" and r["attempts"] >= 3
                        for r in self.store.list("outbox")
                    ),
                },
            }

    def preferences(self, request_id, values, expected=0):
        def save():
            current = self.store.get("preferences", "current") or {}
            existing = {k: current[k] for k in Preferences.model_fields if k in current}
            prefs = Preferences.model_validate({**existing, **values}).model_dump()
            if prefs["schedules_enabled"] and not jobs.ready(
                self.store, prefs["scheduled_delivery_mode"]
            ):
                raise ValueError(
                    "Complete the synthetic account capability tests before enabling schedules"
                )
            return self.store.put(
                "preferences",
                "current",
                {**prefs, "basis": "explicit_learner_setup"},
                expected=expected,
            )

        return self.store.command("preferences", request_id, [values, expected], save)

    def report_context(self, kind, as_of=None, timezone=None, period_as_of=None):
        with self.store.transaction():
            ctx = reports.context(self.store, kind, as_of, timezone, period_as_of)
        # The complete reproducible manifest stays in the persisted snapshot.
        # A conversation receives bounded evidence and topical excerpts.
        return {
            **ctx,
            "evidence_ids": ctx["evidence_ids"][:80],
            "evidence_count": len(ctx["evidence_ids"]),
            "topics": [{**t, "strata": t["strata"][:12]} for t in ctx["topics"][:20]],
            "topic_count": len(ctx["topics"]),
            "retrieval_limit": "Up to 80 evidence IDs and 20 topic cells; the saved snapshot retains the full manifest.",
        }

    def evidence(self, evidence_id):
        if not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= 250:
            raise ValueError("Invalid evidence reference")
        for kind in (
            "attempts",
            "lesson_attempt",
            "context_entry",
            "repair",
            "probe_result",
            "observation",
        ):
            value = self.store.get(kind, evidence_id)
            if value:
                return {
                    "kind": kind,
                    "evidence": value,
                    "instruction_status": "source_data_only",
                }
        raise ValueError("Evidence not found")

    def save_report(
        self,
        request_id,
        snapshot_id,
        narrative=None,
        model_id="deterministic",
        correction_reason=None,
    ):
        return self.store.command(
            "save_report",
            request_id,
            [snapshot_id, narrative, model_id, correction_reason],
            lambda: reports.save(
                self.store, snapshot_id, narrative, model_id, correction_reason
            ),
        )

    def get_report(self, report_id, version=None):
        result = self.store.get("report", report_id, version=version)
        if not result:
            raise ValueError("Report not found")
        return result

    def ready_outputs(self):
        return {
            "outputs": [
                {
                    "job_id": j["id"],
                    "kind": j["kind"],
                    "state": j["state"],
                    "output": j["output"],
                }
                for j in sorted(
                    self.store.list("job"), key=lambda j: j["due_at"], reverse=True
                )
                if j["state"] == "render_ready"
            ][:10],
            "delivery_limit": "Prepared output is not evidence of delivery; native ChatGPT owns notifications.",
        }

    def offer(self, request_id, slot="manual", target=None):
        actual_slot = "manual:" + request_id if slot == "manual" else slot
        if not isinstance(slot, str) or not 1 <= len(slot) <= 200:
            raise ValueError("Invalid logical slot")
        return self.store.command(
            "offer",
            request_id,
            [slot, target],
            lambda: lessons.offer(self.store, actual_slot, target),
        )

    def start(self, request_id, offer_id, condition="unspecified"):
        return self.store.command(
            "start",
            request_id,
            [offer_id, condition],
            lambda: lessons.start(self.store, offer_id, condition),
        )

    def answer(self, request_id, **kwargs):
        return self.store.command(
            "answer", request_id, kwargs, lambda: lessons.answer(self.store, **kwargs)
        )

    def hint(self, request_id, session_id, expected, item_ref):
        return self.store.command(
            "hint",
            request_id,
            [session_id, expected, item_ref],
            lambda: lessons.hint(self.store, session_id, expected, item_ref),
        )

    def lesson_state(self, request_id, session_id, expected, action):
        return self.store.command(
            "lesson_state",
            request_id,
            [session_id, expected, action],
            lambda: lessons.state(self.store, session_id, expected, action),
        )

    def reflection(self, request_id, session_id, text, source="learner_report"):
        if (
            source not in {"learner_report", "tutor_interpretation"}
            or not 1 <= len(text) <= 5000
        ):
            raise ValueError("Invalid reflection")

        def save():
            if not self.store.get("session", session_id):
                raise ValueError("Unknown lesson session")
            return lessons.event(
                self.store, "reflection", session_id, text=text, source=source
            )

        return self.store.command(
            "reflection", request_id, [session_id, text, source], save
        )

    def skip(self, request_id, offer_id):
        def save():
            offer = self.store.get("offer", offer_id)
            if not offer or offer["state"] != "offered":
                raise Conflict("Offer is not current")
            result = self.store.put("offer", offer_id, {**offer, "state": "skipped"})
            lessons.event(self.store, "skipped", offer_id=offer_id)
            return lessons.public_offer(result)

        return self.store.command("skip", request_id, [offer_id], save)

    def upload_context(
        self, request_id, document_id, title, text, expected=0, entries=None
    ):
        return self.store.command(
            "upload_context",
            request_id,
            [document_id, title, text, expected, entries],
            lambda: uploads.upload(
                self.store, document_id, title, text, expected, entries
            ),
        )

    def get_context(self, query="", document_id=None):
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Context query must be at most 200 characters")
        docs = [
            d
            for d in self.store.list("context_document")
            if (not document_id or d["id"] == document_id)
            and (
                not query
                or query.casefold() in (d["title"] + " " + d["text"]).casefold()
            )
        ]
        entries = [
            e
            for e in self.store.list("context_entry")
            if e["document_id"] in {d["id"] for d in docs}
        ]
        return {
            "source_status": "learner_upload_unverified",
            "documents": [
                {
                    "id": d["id"],
                    "version": d["version"],
                    "title": d["title"],
                    "excerpt": d["text"][:4000],
                    "truncated": len(d["text"]) > 4000,
                }
                for d in docs[:5]
            ],
            "matching_documents": len(docs),
            "entries": [
                {**e, "text": e["text"][:2000], "truncated": len(e["text"]) > 2000}
                for e in entries[:20]
            ],
            "matching_entries": len(entries),
        }

    def dispute(self, request_id, item_ref, reason):
        return self.store.command(
            "dispute",
            request_id,
            [item_ref, reason],
            lambda: items.quarantine(self.store, item_ref, reason),
        )

    def due(self, request_id):
        return self.store.command(
            "prepare_due", request_id, [], lambda: jobs.prepare(self.store)
        )

    def generate_job(self, request_id, job_id):
        def generate():
            old = self.store.get("job", job_id)
            if old and old["state"] == "render_ready":
                return old["output"]
            claimed = jobs.claim(self.store, job_id, request_id)
            if not claimed:
                return {"availability": "not_claimable"}
            try:
                if claimed["kind"] == "lesson":
                    output = lessons.offer(self.store, claimed["slot"])
                else:
                    ctx = reports.context(
                        self.store,
                        claimed["kind"],
                        self.store.now(),
                        claimed["timezone"],
                        claimed["due_at"],
                    )
                    output = reports.save(self.store, ctx["snapshot_id"])
            except ValueError:
                jobs.finish(
                    self.store,
                    job_id,
                    request_id,
                    error="Output preparation failed validation; inspect configuration and existing period versions.",
                )
                return {"availability": "preparation_failed", "job_id": job_id}
            jobs.finish(self.store, job_id, request_id, output)
            return output

        return self.store.command("generate_job", request_id, [job_id], generate)

    def delivery(self, request_id, output_id, state, evidence):
        if (
            state not in {"render_ready", "delivery_attempted", "observed_delivered"}
            or not 1 <= len(evidence) <= 3000
        ):
            raise ValueError("Invalid delivery state/evidence")

        def save():
            if not self.store.get("report", output_id) and not self.store.get(
                "offer", output_id
            ):
                raise ValueError("Unknown output")
            return self.store.put(
                "delivery",
                request_id,
                {
                    "output_id": output_id,
                    "state": state,
                    "evidence": evidence,
                    "occurred_at": self.store.now(),
                },
            )

        return self.store.command(
            "delivery", request_id, [output_id, state, evidence], save
        )
