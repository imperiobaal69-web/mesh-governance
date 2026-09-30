"""Mesh governed local store. No inference, paid API, or production-auth claims.

Principals are supplied by trusted host code, never by an action's JSON body.
SQLite is the single-operator implementation; do not expose its file or attach
this library to an unauthenticated mutation endpoint. Published exports remain
exploratory unless their exact revisions have independent human approval.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class ContractError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def require(condition, detail, code="invalid_input"):
    if not condition:
        raise ContractError(code, detail)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class Principal:
    id: str
    capabilities: frozenset[str]
    kind: str = "service"


PUBLIC = Principal("anonymous", frozenset(), "public")
CAPABILITIES = {
    "importSourceVersion": "import", "proposeAssertion": "propose",
    "reviewAssertion": "review", "adjudicateDispute": "adjudicate",
    "correctAssertion": "correct", "invalidateMapping": "correct",
    "publishSnapshot": "publish", "rollbackPublication": "publish",
    "setSourceVisibility": "admin",
}
ALIASES = {"proposeMapping": "proposeAssertion", "reviewMapping": "reviewAssertion"}
DECISIONS = {"supported", "uncertain", "no_mapping", "insufficient_evidence"}
SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
 action TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(actor TEXT, key TEXT, request_hash TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(actor,key));
CREATE TABLE IF NOT EXISTS sources(version TEXT PRIMARY KEY, subject TEXT NOT NULL,
 text TEXT NOT NULL, text_hash TEXT NOT NULL, metadata TEXT NOT NULL, created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_heads(subject TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES sources(version));
CREATE TABLE IF NOT EXISTS source_access(version TEXT PRIMARY KEY REFERENCES sources(version), visibility TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(assertion_id TEXT, revision INTEGER, profile TEXT NOT NULL,
 subject TEXT NOT NULL, payload TEXT NOT NULL, created_seq INTEGER NOT NULL REFERENCES events(seq),
 PRIMARY KEY(assertion_id,revision));
CREATE TABLE IF NOT EXISTS heads(assertion_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(assertion_id,revision) REFERENCES revisions(assertion_id,revision));
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, assertion_id TEXT, revision INTEGER,
 actor TEXT NOT NULL, actor_kind TEXT NOT NULL, verdict TEXT NOT NULL, dimension TEXT NOT NULL,
 reason TEXT NOT NULL, seq INTEGER NOT NULL, FOREIGN KEY(assertion_id,revision) REFERENCES revisions(assertion_id,revision));
CREATE TABLE IF NOT EXISTS cases(assertion_id TEXT, revision INTEGER, case_revision INTEGER NOT NULL,
 state TEXT NOT NULL, resolution TEXT, resolver_kind TEXT, resolved_seq INTEGER,
 PRIMARY KEY(assertion_id,revision), FOREIGN KEY(assertion_id,revision) REFERENCES revisions(assertion_id,revision));
CREATE TABLE IF NOT EXISTS adjudications(id INTEGER PRIMARY KEY, assertion_id TEXT, revision INTEGER,
 case_revision INTEGER NOT NULL, actor TEXT NOT NULL, actor_kind TEXT NOT NULL, outcome TEXT NOT NULL,
 reason TEXT NOT NULL, opinions TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS invalidations(assertion_id TEXT NOT NULL, revision INTEGER NOT NULL,
 reason TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS action_context(seq INTEGER PRIMARY KEY REFERENCES events(seq), context TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, content TEXT NOT NULL, hash TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS active_snapshot(singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL REFERENCES snapshots(id));
CREATE INDEX IF NOT EXISTS review_target ON reviews(assertion_id,revision,seq);
CREATE INDEX IF NOT EXISTS revision_subject ON revisions(subject,created_seq);
"""


class MeshStore:
    def __init__(self, path, concepts):
        self.concepts = {x["uri"]: copy.deepcopy(x) for x in concepts}
        require(len(self.concepts) == len(concepts), "Duplicate concept identities")
        self.taxonomy_hash = digest(concepts)
        # Validate before creating a database or pinning an unusable vocabulary.
        for uri in self.concepts:
            self.ancestor_paths(uri)
        self.db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            if str(path) != ":memory:":
                os.chmod(path, 0o600)
            # Pin the vocabulary and install its guards together. The write lock
            # also prevents simultaneous initializers from choosing different hashes.
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            old = self.db.execute("SELECT value FROM configuration WHERE key='taxonomy_hash'").fetchone()
            require(old is None or old[0] == self.taxonomy_hash, "Taxonomy changed; use an explicit migration/new store", "stale_dependency")
            self.db.execute("INSERT OR IGNORE INTO configuration VALUES('taxonomy_hash',?)", (self.taxonomy_hash,))
            for table in ("events", "commands", "sources", "revisions", "reviews", "adjudications", "invalidations", "snapshots", "action_context"):
                for verb in ("UPDATE", "DELETE"):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable record'); END")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def seq(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]

    def event(self, actor, action, payload):
        return self.db.execute("INSERT INTO events(actor,action,at,payload) VALUES(?,?,?,?)",
                               (actor.id, action, utc(), canonical(payload))).lastrowid

    def source(self, version, actor):
        row = self.db.execute("SELECT s.*,a.visibility FROM sources s JOIN source_access a USING(version) WHERE version=?", (version,)).fetchone()
        require(row is not None and (row["visibility"] == "public" or "read_private" in actor.capabilities),
                "Source unavailable to this actor", "permission")
        return dict(row)

    def execute(self, actor, request):
        require(isinstance(actor, Principal) and nonempty(actor.id), "Trusted principal required", "permission")
        require(isinstance(request, dict) and set(request) == {"api_version", "action", "object_id", "expected_revision", "idempotency_key", "parameters"}, "Invalid action envelope")
        action = ALIASES.get(request["action"], request["action"])
        require(action in CAPABILITIES and CAPABILITIES[action] in actor.capabilities, "Capability required", "permission")
        require(request["api_version"] == "mesh.actions.v1", "Unsupported action API")
        require(nonempty(request["idempotency_key"]) and len(request["idempotency_key"]) <= 200, "Invalid idempotency key")
        require(isinstance(request["parameters"], dict), "Parameters must be an object")
        require(type(request["expected_revision"]) is int and request["expected_revision"] >= 0, "Expected revision must be a nonnegative integer")
        require(nonempty(request["object_id"]), "Object identity required")
        # Persist only canonical JSON; NaN/Infinity and arbitrary Python types fail.
        fingerprint = digest(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT * FROM commands WHERE actor=? AND key=?", (actor.id, request["idempotency_key"])).fetchone()
            if prior:
                require(prior["request_hash"] == fingerprint, "Idempotency key reused with different content", "version_conflict")
                result = json.loads(prior["response"])
            else:
                result = getattr(self, "_" + action)(actor, request)
                self.db.execute("INSERT INTO commands VALUES(?,?,?,?)", (actor.id, request["idempotency_key"], fingerprint, canonical(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _importSourceVersion(self, actor, req):
        p = req["parameters"]
        require(set(p) == {"subject", "text", "metadata", "visibility"}, "Invalid source fields")
        require(nonempty(p["subject"]) and isinstance(p["text"], str) and isinstance(p["metadata"], dict), "Invalid source")
        require(p["visibility"] in {"public", "private"}, "Invalid visibility")
        version = digest({"subject": p["subject"], "text": p["text"], "metadata": p["metadata"]})
        old = self.db.execute("SELECT * FROM sources WHERE version=?", (version,)).fetchone()
        if old:
            self.source(version, actor)
            return {"source_version": version, "event_id": old["created_seq"], "duplicate": True}
        require(req["expected_revision"] == 0, "Source versions are immutable")
        seq = self.event(actor, "importSourceVersion", {"subject": p["subject"], "source_version": version})
        th = hashlib.sha256(p["text"].encode()).hexdigest()
        self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?)", (version, p["subject"], p["text"], th, canonical(p["metadata"]), seq))
        self.db.execute("INSERT INTO source_access VALUES(?,?)", (version, p["visibility"]))
        self.db.execute("INSERT INTO source_heads VALUES(?,?) ON CONFLICT(subject) DO UPDATE SET version=excluded.version", (p["subject"], version))
        affected = self.db.execute("SELECT r.* FROM revisions r JOIN heads h USING(assertion_id,revision) WHERE r.subject=?", (p["subject"],)).fetchall()
        for row in affected:
            self.db.execute("UPDATE heads SET stale=1 WHERE assertion_id=?", (row["assertion_id"],))
            self.db.execute("INSERT INTO invalidations VALUES(?,?,?,?)", (row["assertion_id"], row["revision"], "source_changed: reevaluation required", seq))
        return {"source_version": version, "event_id": seq, "invalidated_assertions": len(affected)}

    def _setSourceVisibility(self, actor, req):
        require(req["parameters"].get("visibility") in {"public", "private"}, "Invalid visibility")
        self.source(req["object_id"], actor)
        self.db.execute("UPDATE source_access SET visibility=? WHERE version=?", (req["parameters"]["visibility"], req["object_id"]))
        seq = self.event(actor, "setSourceVisibility", {"source_version": req["object_id"], "visibility": req["parameters"]["visibility"]})
        return {"event_id": seq}

    def validate_time(self, time):
        require(isinstance(time, dict) and time.get("kind") in {"unknown", "not_applicable", "instant", "interval"}, "Invalid temporal kind")
        kind = time["kind"]
        keys = {"kind", "at"} if kind == "instant" else {"kind", "from", "to"} if kind == "interval" else {"kind"}
        require(set(time) == keys, "Unexpected temporal fields; unknown is not infinity")
        for key in keys - {"kind"}:
            self.timestamp(time[key])
        if kind == "interval":
            require(self.timestamp(time["from"]) < self.timestamp(time["to"]), "Empty/reversed interval")

    @staticmethod
    def timestamp(value):
        require(nonempty(value), "Time boundary required")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("invalid_input", "Use ISO8601 timestamps") from exc
        require(parsed.utcoffset() is not None, "Time zone required")
        require(parsed.utcoffset().total_seconds() == 0, "Use explicit UTC boundaries")
        return parsed.timestamp()

    def validate(self, payload, actor, current=True):
        require(isinstance(payload, dict), "Assertion must be an object")
        required = {"profile", "subject", "predicate", "object", "source_version", "evidence", "semantic_state", "modality", "valid_time", "detail"}
        require(set(payload) == required, "Invalid assertion fields")
        require(payload["semantic_state"] in DECISIONS, "Unknown semantic state")
        require(payload["profile"] in {"cordis-sdg", "observation"}, "Unregistered domain profile")
        source = self.source(payload["source_version"], actor)
        require(source["subject"] == payload["subject"], "Source belongs to a different subject")
        if current:
            head = self.db.execute("SELECT version FROM source_heads WHERE subject=?", (payload["subject"],)).fetchone()
            require(head and head[0] == payload["source_version"], "Source superseded", "stale_dependency")
        self.validate_time(payload["valid_time"])
        require(isinstance(payload["detail"], dict) and isinstance(payload["evidence"], list), "Invalid assertion details/evidence")
        require(payload["semantic_state"] != "supported" or bool(payload["evidence"]), "Supported assertions require evidence")
        for ref in payload["evidence"]:
            require(isinstance(ref, dict), "Invalid selector")
            if ref.get("kind") == "text":
                require(set(ref) == {"kind", "start", "end", "quote", "text_hash"}, "Invalid text selector")
                a, b = ref["start"], ref["end"]
                require(type(a) is int and type(b) is int and 0 <= a < b <= len(source["text"]), "Invalid evidence range")
                require(source["text"][a:b] == ref["quote"] and ref["text_hash"] == source["text_hash"], "Evidence/hash mismatch")
            elif ref.get("kind") == "field":
                require(set(ref) == {"kind", "field", "value", "text_hash"}, "Invalid structured selector")
                try:
                    value = json.loads(source["text"])[ref["field"]]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ContractError("invalid_input", "Structured evidence field does not exist") from exc
                require(canonical(value) == canonical(ref["value"]) and ref["text_hash"] == source["text_hash"], "Structured evidence mismatch")
            else:
                raise ContractError("invalid_input", "Unregistered evidence selector")
        if payload["profile"] == "cordis-sdg":
            require(payload["predicate"] == "documented_contribution", "Unregistered research predicate")
            require(isinstance(payload["object"], str) and payload["object"] in self.concepts, "Unknown official concept")
            require(payload["modality"] in {None, "planned", "reported_activity", "reported_result", "reported_impact"}, "Unknown contribution modality")
            require(payload["semantic_state"] != "supported" or payload["modality"] is not None, "Supported contribution requires modality")
            require(all(x["kind"] == "text" for x in payload["evidence"]), "Research profile requires text selectors")
        else:
            require(payload["predicate"] == "reported_temperature" and payload["modality"] == "reported_measurement", "Unregistered observation predicate/modality")
            obj = payload["object"]
            require(isinstance(obj, dict) and set(obj) == {"value", "unit"}, "Quantity requires value and unit")
            require(type(obj["value"]) in {int, float} and math.isfinite(obj["value"]) and obj["unit"] == "Cel", "Only finite Celsius measurements in this profile")
            require(any(x["kind"] == "field" and x["field"] == "temperature" and x["value"] == obj["value"] for x in payload["evidence"]), "Measurement must match the structured source")

    def head(self, aid, actor):
        row = self.db.execute("SELECT r.*,h.stale FROM revisions r JOIN heads h USING(assertion_id,revision) WHERE assertion_id=?", (aid,)).fetchone()
        require(row is not None, "Assertion not found", "not_found")
        payload = json.loads(row["payload"])
        self.source(payload["source_version"], actor)
        return dict(row), payload

    def write_revision(self, actor, aid, revision, payload, action, reason=None):
        seq = self.event(actor, action, {"assertion_id": aid, "revision": revision, "reason": reason})
        self.db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (aid, revision, payload["profile"], payload["subject"], canonical(payload), seq))
        self.db.execute("INSERT INTO heads VALUES(?,?,0) ON CONFLICT(assertion_id) DO UPDATE SET revision=excluded.revision,stale=0", (aid, revision))
        self.db.execute("INSERT INTO cases VALUES(?,?,0,'unreviewed',NULL,NULL,NULL)", (aid, revision))
        return {"assertion_id": aid, "revision": revision, "event_id": seq}

    def proposer(self, aid, revision):
        row = self.db.execute("SELECT e.actor FROM revisions r JOIN events e ON e.seq=r.created_seq WHERE r.assertion_id=? AND r.revision=?", (aid, revision)).fetchone()
        return row["actor"] if row else None

    def context(self, seq, p, allowed):
        """Split optional provenance fields (independence, entered_by) from the action parameters."""
        extra = {k: p[k] for k in allowed if k in p}
        require(extra.get("independence", "blind") in {"blind", "informed"}, "Invalid independence")
        require("entered_by" not in extra or nonempty(extra["entered_by"]), "Invalid entered_by")
        if extra:
            self.db.execute("INSERT INTO action_context VALUES(?,?)", (seq, canonical(extra)))
        return {k: v for k, v in p.items() if k not in allowed}

    def _proposeAssertion(self, actor, req):
        require(req["expected_revision"] == 0, "New assertion starts at revision zero")
        self.validate(req["parameters"], actor)
        existing = self.db.execute("SELECT * FROM revisions WHERE assertion_id=? AND revision=1", (req["object_id"],)).fetchone()
        if existing:
            require(existing["payload"] == canonical(req["parameters"]), "Assertion identity reused", "version_conflict")
            return {"assertion_id": req["object_id"], "revision": 1, "event_id": existing["created_seq"], "duplicate": True}
        return self.write_revision(actor, req["object_id"], 1, req["parameters"], "proposeAssertion")

    def _correctAssertion(self, actor, req):
        row, old = self.head(req["object_id"], actor)
        require(row["revision"] == req["expected_revision"], "Assertion revision changed", "version_conflict")
        p = req["parameters"]
        require(set(p) == {"replacement", "reason"} and nonempty(p["reason"]), "Correction requires replacement and reason")
        self.validate(p["replacement"], actor)
        for field in ("profile", "subject", "predicate"):
            require(old[field] == p["replacement"][field], "Correction cannot change assertion identity")
        if old["profile"] == "cordis-sdg":
            require(old["object"] == p["replacement"]["object"], "Use a new assertion for a different concept")
        return self.write_revision(actor, req["object_id"], row["revision"] + 1, p["replacement"], "correctAssertion", p["reason"])

    def _invalidateMapping(self, actor, req):
        row, _ = self.head(req["object_id"], actor)
        require(row["revision"] == req["expected_revision"], "Assertion revision changed", "version_conflict")
        require(nonempty(req["parameters"].get("reason")), "Invalidation requires a reason")
        seq = self.event(actor, "invalidateMapping", {"assertion_id": req["object_id"], **req["parameters"]})
        self.db.execute("UPDATE heads SET stale=1 WHERE assertion_id=?", (req["object_id"],))
        self.db.execute("INSERT INTO invalidations VALUES(?,?,?,?)", (req["object_id"], row["revision"], req["parameters"]["reason"], seq))
        return {"event_id": seq}

    def _reviewAssertion(self, actor, req):
        row, _ = self.head(req["object_id"], actor)
        require(row["revision"] == req["expected_revision"], "Review must name the exact current revision", "version_conflict")
        require(not row["stale"], "Review cannot approve stale evidence", "stale_dependency")
        p = req["parameters"]
        require(actor.id != self.proposer(req["object_id"], row["revision"]), "The proposer cannot review its own assertion", "independence")
        extra = {k: p[k] for k in ("independence", "entered_by") if k in p}
        p = {k: v for k, v in p.items() if k not in extra}
        require(set(p) == {"verdict", "dimension", "reason"} and p["verdict"] in {"uphold", "reject", "request_evidence"} and nonempty(p["reason"]), "Invalid review")
        require(p["dimension"] in {"activity", "concept", "scope", "modality", "evidence", "time", "identity", "granularity"}, "Invalid dispute dimension")
        seq = self.event(actor, "reviewAssertion", {"assertion_id": req["object_id"], "revision": row["revision"], **p, **extra})
        self.context(seq, extra, ("independence", "entered_by"))
        self.db.execute("INSERT INTO reviews(assertion_id,revision,actor,actor_kind,verdict,dimension,reason,seq) VALUES(?,?,?,?,?,?,?,?)",
                        (req["object_id"], row["revision"], actor.id, actor.kind, p["verdict"], p["dimension"], p["reason"], seq))
        reviews = self.opinions(req["object_id"], row["revision"])
        oldcase = self.db.execute("SELECT * FROM cases WHERE assertion_id=? AND revision=?", (req["object_id"], row["revision"])).fetchone()
        # Same-actor changes preserve history but only the latest opinion is effective.
        state = "open" if len({x["verdict"] for x in reviews}) > 1 or oldcase["resolution"] else "reviewed"
        self.db.execute("UPDATE cases SET case_revision=case_revision+1,state=?,resolution=NULL,resolver_kind=NULL,resolved_seq=NULL WHERE assertion_id=? AND revision=?", (state, req["object_id"], row["revision"]))
        return {"event_id": seq, "revision": row["revision"], "case_revision": oldcase["case_revision"] + 1, "case_state": state}

    def opinions(self, aid, revision):
        rows = self.db.execute("SELECT * FROM reviews WHERE assertion_id=? AND revision=? ORDER BY seq", (aid, revision)).fetchall()
        latest = {x["actor"]: dict(x) for x in rows}
        return list(latest.values())

    def _adjudicateDispute(self, actor, req):
        row, _ = self.head(req["object_id"], actor)
        require(req["expected_revision"] == row["revision"] and not row["stale"], "Stale adjudication target", "version_conflict")
        case = self.db.execute("SELECT * FROM cases WHERE assertion_id=? AND revision=?", (req["object_id"], row["revision"])).fetchone()
        p = req["parameters"]
        extra = {k: p[k] for k in ("entered_by",) if k in p}
        p = {k: v for k, v in p.items() if k not in extra}
        require(set(p) == {"expected_case_revision", "opinion_ids", "outcome", "reason"}, "Invalid adjudication")
        require(type(p["expected_case_revision"]) is int and p["expected_case_revision"] == case["case_revision"], "New opinion or adjudication changed the case", "version_conflict")
        opinions = self.opinions(req["object_id"], row["revision"])
        require(bool(opinions) and p["opinion_ids"] == sorted(x["id"] for x in opinions), "Adjudication must consider every current opinion", "stale_dependency")
        require(actor.id != self.proposer(req["object_id"], row["revision"]) and actor.id not in {x["actor"] for x in opinions},
                "The adjudicator cannot be the proposer or an opinion author", "independence")
        require(p["outcome"] in {"uphold", "reject", "request_evidence", "unresolved"} and nonempty(p["reason"]), "Invalid outcome")
        seq = self.event(actor, "adjudicateDispute", {"assertion_id": req["object_id"], "revision": row["revision"], **p, **extra})
        self.context(seq, extra, ("entered_by",))
        self.db.execute("INSERT INTO adjudications(assertion_id,revision,case_revision,actor,actor_kind,outcome,reason,opinions,seq) VALUES(?,?,?,?,?,?,?,?,?)",
                        (req["object_id"], row["revision"], case["case_revision"], actor.id, actor.kind, p["outcome"], p["reason"], canonical(p["opinion_ids"]), seq))
        state = "adjudicated" if p["outcome"] in {"uphold", "reject"} else "unresolved"
        self.db.execute("UPDATE cases SET case_revision=case_revision+1,state=?,resolution=?,resolver_kind=?,resolved_seq=? WHERE assertion_id=? AND revision=?",
                        (state, p["outcome"], actor.kind, seq, req["object_id"], row["revision"]))
        return {"event_id": seq, "case_revision": case["case_revision"] + 1, "case_state": state}

    def review_state(self, aid, revision):
        case = self.db.execute("SELECT * FROM cases WHERE assertion_id=? AND revision=?", (aid, revision)).fetchone()
        if case["state"] in {"open", "unresolved"}:
            return "disputed"
        if case["resolution"]:
            return "rejected" if case["resolution"] == "reject" else "human_approved" if case["resolver_kind"] == "human" else "ai_reviewed"
        opinions = self.opinions(aid, revision)
        if not opinions:
            return "unreviewed"
        if any(x["verdict"] == "reject" for x in opinions):
            # A rejection needs a human: an AI-only rejection opens a dispute for adjudication.
            return "rejected" if any(x["verdict"] == "reject" and x["actor_kind"] == "human" for x in opinions) else "disputed"
        if any(x["verdict"] == "request_evidence" for x in opinions):
            return "needs_evidence"
        return "human_approved" if any(x["actor_kind"] == "human" for x in opinions) else "ai_reviewed"

    def query(self, actor, aid, known_at=None, valid_at=None):
        require(known_at is None or type(known_at) is int and 0 <= known_at <= self.seq(), "Invalid committed cutoff")
        cutoff = self.seq() if known_at is None else known_at
        row = self.db.execute("SELECT * FROM revisions WHERE assertion_id=? AND created_seq<=? ORDER BY revision DESC LIMIT 1", (aid, cutoff)).fetchone()
        require(row is not None, "No assertion at this cutoff", "not_found")
        p = json.loads(row["payload"])
        self.source(p["source_version"], actor)  # Current ACL also applies to old revisions.
        time = p["valid_time"]
        applicability = "not_requested"
        if valid_at is not None:
            point = self.timestamp(valid_at)
            applicability = "temporal_unknown" if time["kind"] in {"unknown", "not_applicable"} else "applicable" if (
                self.timestamp(time["at"]) == point if time["kind"] == "instant" else self.timestamp(time["from"]) <= point < self.timestamp(time["to"])) else "outside_interval"
        nextrow = self.db.execute("SELECT created_seq FROM revisions WHERE assertion_id=? AND revision>? AND created_seq<=? ORDER BY revision LIMIT 1", (aid, row["revision"], cutoff)).fetchone()
        invalidated = self.db.execute("SELECT reason FROM invalidations WHERE assertion_id=? AND revision=? AND seq<=? ORDER BY seq DESC LIMIT 1", (aid, row["revision"], cutoff)).fetchone()
        return {"assertion_id": aid, "revision": row["revision"], "known_at": cutoff, "recorded_from": row["created_seq"], "recorded_to": nextrow[0] if nextrow else None,
                "temporal_state": applicability, "dependency_state": "stale" if invalidated else "current_at_cutoff", "invalidation_reason": invalidated[0] if invalidated else None, "payload": p}

    def ancestor_paths(self, uri):
        require(uri in self.concepts, "Unknown concept")
        result, queue = [], [(uri, [uri])]
        while queue:
            node, path = queue.pop()
            require(len(result) + len(queue) < 10000, "Taxonomy traversal limit reached")
            for parent in self.concepts[node].get("broader", []):
                require(parent in self.concepts and parent not in path, "Dangling/cyclic taxonomy")
                route = path + [parent]
                result.append(route)
                queue.append((parent, route))
        return result

    def derive_navigation(self, actor, aids):
        conclusions = {}
        for aid in dict.fromkeys(aids):
            row, p = self.head(aid, actor)
            if row["stale"] or p["profile"] != "cordis-sdg" or p["semantic_state"] != "supported" or self.review_state(aid, row["revision"]) != "human_approved":
                continue
            for path in self.ancestor_paths(p["object"]):
                key = (p["subject"], path[-1])
                conclusion = conclusions.setdefault(key, {"subject": p["subject"], "concept": path[-1], "origin": "derived_navigation", "justifications": []})
                conclusion["justifications"].append({"rule": "R1.1", "rule_hash": digest({"rule": "R1.1", "meaning": "navigation_only_no_impact"}), "assertion_id": aid,
                                                    "revision": row["revision"], "source_version": p["source_version"], "taxonomy_hash": self.taxonomy_hash, "path": path, "modality": p["modality"]})
        return list(conclusions.values())

    def _publishSnapshot(self, actor, req):
        p = req["parameters"]
        require(set(p) == {"assertion_ids", "source_versions", "mode", "expected_seq", "operational_findings"}, "Invalid publication manifest")
        require(p["mode"] in {"exploratory", "reviewed"} and type(p["expected_seq"]) is int and p["expected_seq"] == self.seq(), "Publication cutoff changed", "version_conflict")
        require(isinstance(p["assertion_ids"], list) and len(p["assertion_ids"]) == len(set(p["assertion_ids"])), "Duplicate assertions")
        require(isinstance(p["operational_findings"], list), "Operational findings must be a list")
        require(isinstance(p["source_versions"], list) and len(p["source_versions"]) == len(set(p["source_versions"])), "Duplicate sources")
        sources = {}
        for version in p["source_versions"]:
            source = self.source(version, actor)
            require(source["visibility"] == "public", "Private evidence cannot enter a public snapshot", "permission")
            head = self.db.execute("SELECT version FROM source_heads WHERE subject=?", (source["subject"],)).fetchone()
            require(head[0] == version, "Publication contains a superseded source", "stale_dependency")
            sources[version] = {"subject": source["subject"], "text": source["text"], "text_hash": source["text_hash"], "metadata": json.loads(source["metadata"])}
        records = []
        for aid in p["assertion_ids"]:
            row, payload = self.head(aid, actor)
            require(not row["stale"], "Stale assertion cannot be published", "stale_dependency")
            require(payload["source_version"] in sources, "Publication missing source")
            self.validate(payload, actor)
            review = self.review_state(aid, row["revision"])
            if p["mode"] == "reviewed":
                require(review == "human_approved", "Reviewed publication requires exact-revision human approval", "quality_gate")
            records.append({"assertion_id": aid, "revision": row["revision"], "review_status": review, "payload": payload})
        require(all(isinstance(x, dict) and x.get("source_version") in sources and x.get("processing_state") in {"not_assessed", "processing_error"} and x.get("semantic_decision") is None and nonempty(x.get("reason")) for x in p["operational_findings"]), "Invalid operational finding")
        require(p["mode"] != "reviewed" or not p["operational_findings"], "Reviewed collection has unresolved assessments", "quality_gate")
        body = {"schema": "mesh.snapshot.v1", "mode": p["mode"], "cutoff": self.seq(), "taxonomy_hash": self.taxonomy_hash, "concepts": list(self.concepts.values()),
                "sources": sources, "assertions": records, "operational_findings": p["operational_findings"], "derivations": self.derive_navigation(actor, p["assertion_ids"])}
        sh = digest(body)
        sid = "snapshot:" + sh
        seq = self.event(actor, "publishSnapshot", {"snapshot_id": sid, "mode": p["mode"], "hash": sh})
        self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)", (sid, canonical(body), sh, seq))
        self.db.execute("INSERT INTO active_snapshot VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET id=excluded.id", (sid,))
        return {"snapshot_id": sid, "sha256": sh, "event_id": seq}

    def snapshot(self, actor=PUBLIC, snapshot_id=None):
        if snapshot_id is None:
            active = self.db.execute("SELECT id FROM active_snapshot WHERE singleton=1").fetchone()
            require(active is not None, "No published snapshot", "not_found")
            snapshot_id = active[0]
        row = self.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        require(row is not None, "Snapshot not found", "not_found")
        body = json.loads(row["content"])
        require(digest(body) == row["hash"], "Snapshot hash mismatch", "integrity")
        allowed = {}
        for version, source in body["sources"].items():
            try:
                self.source(version, actor)
                allowed[version] = source
            except ContractError as exc:
                if exc.code != "permission":
                    raise
        body["sources"] = allowed
        body["assertions"] = [x for x in body["assertions"] if x["payload"]["source_version"] in allowed]
        body["operational_findings"] = [x for x in body["operational_findings"] if x["source_version"] in allowed]
        eligible = {(x["assertion_id"], x["revision"]) for x in body["assertions"]}
        derived = []
        for item in body["derivations"]:
            item["justifications"] = [x for x in item["justifications"] if (x["assertion_id"], x["revision"]) in eligible]
            if item["justifications"]:
                derived.append(item)
        body["derivations"] = derived
        return {"snapshot_id": snapshot_id, "original_sha256": row["hash"], "view_sha256": digest(body), "historical_publication": True, **body}

    def _rollbackPublication(self, actor, req):
        body = self.snapshot(actor, req["object_id"])
        # Rollback must revalidate ALL original resources, not a permission-filtered view.
        row = self.db.execute("SELECT content FROM snapshots WHERE id=?", (req["object_id"],)).fetchone()
        original = json.loads(row[0])
        require(set(body["sources"]) == set(original["sources"]), "Rollback source access revoked", "permission")
        for version in original["sources"]:
            require(self.source(version, actor)["visibility"] == "public", "Rollback cannot republish restricted source", "permission")
        for item in original["assertions"]:
            current, _ = self.head(item["assertion_id"], actor)
            require(not current["stale"] and current["revision"] == item["revision"], "Rollback would restore stale knowledge", "stale_dependency")
            require(self.review_state(item["assertion_id"], item["revision"]) == item["review_status"], "Review changed since publication", "stale_dependency")
        self.db.execute("UPDATE active_snapshot SET id=? WHERE singleton=1", (req["object_id"],))
        return {"snapshot_id": req["object_id"], "event_id": self.event(actor, "rollbackPublication", {"snapshot_id": req["object_id"]})}


def command(action, oid, parameters, revision=0, key=None):
    return {"api_version": "mesh.actions.v1", "action": action, "object_id": oid, "expected_revision": revision,
            "idempotency_key": key or digest([action, oid, revision, parameters]), "parameters": parameters}
