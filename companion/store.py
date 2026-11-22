"""Transactional versioned ledger shared by SQLite and PostgreSQL.

The deployment is single-learner. All writers serialize before reading state;
immutable entity versions and acknowledgements commit in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from study_sync import digest, encode, iso, timestamp, utcnow


class Conflict(ValueError):
    """The caller must refresh state or resolve a source conflict."""


class Store:
    def __init__(self, location, learner="local-learner", clock=utcnow):
        self.location, self.learner, self.clock = str(location), learner, clock
        self.postgres = self.location.startswith(("postgresql://", "postgres://"))
        if self.postgres:
            import psycopg

            self.db = psycopg.connect(self.location, autocommit=True)
        else:
            Path(location).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(location, timeout=30, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
        self._active = False
        with self.transaction():
            self.sql(
                "CREATE TABLE IF NOT EXISTS companion_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.sql(
                "CREATE TABLE IF NOT EXISTS entity_versions (seq BIGINT PRIMARY KEY, kind TEXT NOT NULL, "
                "id TEXT NOT NULL, version INTEGER NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL, "
                "UNIQUE(kind,id,version))"
            )
            self.sql(
                "CREATE INDEX IF NOT EXISTS entity_lookup ON entity_versions(kind,id,version)"
            )
            self.sql(
                "CREATE TABLE IF NOT EXISTS acknowledgements (operation TEXT NOT NULL, key TEXT NOT NULL, "
                "request_hash TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(operation,key))"
            )
            old = self.sql(
                "SELECT value FROM companion_meta WHERE key='learner'"
            ).fetchone()
            if old and old[0] != learner:
                raise ValueError("Use a separate companion database for each learner")
            self.sql(
                "INSERT INTO companion_meta VALUES ('learner',?) ON CONFLICT(key) DO NOTHING",
                (learner,),
            )
            self.sql(
                "INSERT INTO companion_meta VALUES ('schema','2') ON CONFLICT(key) DO NOTHING"
            )

    def sql(self, query, args=()):
        return self.db.execute(
            query.replace("?", "%s") if self.postgres else query, args
        )

    @contextmanager
    def transaction(self):
        if self._active:
            raise RuntimeError("Nested companion transactions are not supported")
        self.sql("BEGIN" if self.postgres else "BEGIN IMMEDIATE")
        self._active = True
        try:
            if self.postgres:
                self.sql("SELECT pg_advisory_xact_lock(70912026)")
            yield self
            self.sql("COMMIT")
        except BaseException:
            self.sql("ROLLBACK")
            raise
        finally:
            self._active = False

    def close(self):
        self.db.close()

    def now(self):
        return iso(self.clock())

    def revision(self):
        return self.sql("SELECT COALESCE(MAX(seq),0) FROM entity_versions").fetchone()[
            0
        ]

    def evidence_revision(self, cutoff):
        kinds = (
            "attempts",
            "assessments",
            "assessment",
            "activities",
            "capture",
            "preferences",
            "context_document",
            "context_entry",
            "observation",
            "lesson_attempt",
            "lesson_event",
            "offer",
            "repair",
            "probe_result",
            "item",
            "exposure",
        )
        placeholders = ",".join("?" for _ in kinds)
        return self.sql(
            "SELECT COALESCE(MAX(seq),0) FROM entity_versions WHERE kind IN ("
            + placeholders
            + ") AND received_at<=?",
            (*kinds, iso(timestamp(cutoff))),
        ).fetchone()[0]

    def get(self, kind, identifier, cutoff=None, version=None):
        query, args = (
            "SELECT body FROM entity_versions WHERE kind=? AND id=?",
            [kind, identifier],
        )
        if cutoff:
            query += " AND received_at<=?"
            args.append(iso(timestamp(cutoff)))
        if version is not None:
            query += " AND version=?"
            args.append(version)
        row = self.sql(query + " ORDER BY version DESC LIMIT 1", args).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind, cutoff=None):
        query, args = "SELECT id,body FROM entity_versions WHERE kind=?", [kind]
        if cutoff:
            query += " AND received_at<=?"
            args.append(iso(timestamp(cutoff)))
        # Only the newest version available at the historical cutoff is visible.
        rows = self.sql(query + " ORDER BY version,seq", args).fetchall()
        return list({row[0]: json.loads(row[1]) for row in rows}.values())

    def put(self, kind, identifier, body, expected=None, received_at=None):
        if not self._active:
            raise RuntimeError("Writes require a transaction")
        old = self.get(kind, identifier)
        version = old["version"] if old else 0
        if expected is not None and expected != version:
            raise Conflict("Stale version; refresh before retrying")
        result = {**body, "id": identifier, "version": version + 1}
        received = received_at or self.now()
        result["received_at"] = iso(timestamp(received))
        self.sql(
            "INSERT INTO entity_versions VALUES (?,?,?,?,?,?)",
            (
                self.revision() + 1,
                kind,
                identifier,
                version + 1,
                result["received_at"],
                encode(result),
            ),
        )
        return result

    def command(self, operation, key, request, action):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError("A stable request_id of 1..200 characters is required")
        with self.transaction():
            old = self.sql(
                "SELECT request_hash,response FROM acknowledgements WHERE operation=? AND key=?",
                (operation, key),
            ).fetchone()
            if old:
                if old[0] != digest(request):
                    raise Conflict(
                        "request_id was already used with different arguments"
                    )
                return json.loads(old[1])
            result = action()
            self.sql(
                "INSERT INTO acknowledgements VALUES (?,?,?,?)",
                (operation, key, digest(request), encode(result)),
            )
        # Feedback is only returned after the commit above succeeds.
        return result

    def backup(self, destination):
        """Portable logical backup including keys; keep this in private storage."""
        from study_sync import atomic_json

        with self.transaction():
            data = {
                "schema_version": 2,
                "learner": self.learner,
                "versions": self.sql(
                    "SELECT * FROM entity_versions ORDER BY seq"
                ).fetchall(),
                "acknowledgements": self.sql(
                    "SELECT * FROM acknowledgements ORDER BY operation,key"
                ).fetchall(),
            }
        atomic_json(destination, data)
        return {"versions": len(data["versions"]), "sha256": digest(data)}

    def restore(self, data):
        if data.get("schema_version") != 2 or data.get("learner") != self.learner:
            raise ValueError("Backup schema/learner mismatch")
        with self.transaction():
            if self.revision():
                raise ValueError("Restore requires an empty database")
            for row in data["versions"]:
                self.sql("INSERT INTO entity_versions VALUES (?,?,?,?,?,?)", row)
            for row in data["acknowledgements"]:
                self.sql("INSERT INTO acknowledgements VALUES (?,?,?,?)", row)
