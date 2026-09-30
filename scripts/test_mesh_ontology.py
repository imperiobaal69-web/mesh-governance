"""Adversarial acceptance checks for the LOCAL governance implementation."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from mesh_ontology import MeshStore, Principal, PUBLIC, ContractError, command

CONCEPTS = [{"uri": "http://data.europa.eu/sdg/6", "code": "6", "types": ["Goal"], "broader": [], "labels": {"en": "Water"}},
            {"uri": "http://data.europa.eu/sdg/6.3", "code": "6.3", "types": ["Target"], "broader": ["http://data.europa.eu/sdg/6"], "labels": {"en": "Water quality"}}]
ADMIN = Principal("local-admin", frozenset({"import", "propose", "correct", "publish", "admin", "read_private"}))
WORKER = Principal("worker", frozenset({"propose"}), "model")
HUMAN = Principal("reviewer-a", frozenset({"review"}), "human")
OTHER = Principal("reviewer-b", frozenset({"review"}), "human")
AI = Principal("assistant", frozenset({"review"}), "ai")
JUDGE = Principal("adjudicator", frozenset({"adjudicate"}), "human")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "knowledge.sqlite"
        self.s = MeshStore(self.path, CONCEPTS)
        self.text = "α Water treatment is planned. No achieved impact has been measured."
        self.source = self.import_source(self.text)
        self.payload = self.assertion(self.source, self.text)
        self.s.execute(WORKER, command("proposeAssertion", "a", self.payload))

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def import_source(self, text, subject="P"):
        return self.s.execute(ADMIN, command("importSourceVersion", subject, {"subject": subject, "text": text, "metadata": {"project": {"id": subject}}, "visibility": "public"}))["source_version"]

    def assertion(self, version, text):
        return {"profile": "cordis-sdg", "subject": "P", "predicate": "documented_contribution", "object": CONCEPTS[1]["uri"], "source_version": version,
                "evidence": [{"kind": "text", "start": 0, "end": len(text), "quote": text, "text_hash": hashlib.sha256(text.encode()).hexdigest()}],
                "semantic_state": "supported", "modality": "planned", "valid_time": {"kind": "unknown"}, "detail": {"claim": "Plans water treatment."}}

    def review(self, who=HUMAN, verdict="uphold", aid="a", revision=1, key=None):
        return self.s.execute(who, command("reviewAssertion", aid, {"verdict": verdict, "dimension": "scope", "reason": "Read the complete source; assessed the stated activity and scope."}, revision, key))

    def publish(self, mode="exploratory", aids=None, sources=None, findings=None):
        return self.s.execute(ADMIN, command("publishSnapshot", "collection", {"assertion_ids": aids or ["a"], "source_versions": sources or [self.source], "mode": mode,
                                                                           "expected_seq": self.s.seq(), "operational_findings": findings or []}))

    def fails(self, code, fn):
        with self.assertRaises(ContractError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_idempotency_replay_has_no_extra_event(self):
        req = command("reviewAssertion", "a", {"verdict": "uphold", "dimension": "scope", "reason": "Read source."}, 1, "one")
        first = self.s.execute(HUMAN, req)
        seq = self.s.seq()
        self.assertEqual(self.s.execute(HUMAN, req), first)
        self.assertEqual(self.s.seq(), seq)

    def test_idempotency_parameter_collision_is_conflict(self):
        self.review(key="one")
        self.fails("version_conflict", lambda: self.review(verdict="reject", key="one"))

    def test_idempotency_is_scoped_to_actor(self):
        self.review(HUMAN, key="same")
        self.review(OTHER, key="same")
        self.assertEqual(len(self.s.opinions("a", 1)), 2)

    def test_same_source_does_not_duplicate(self):
        self.assertEqual(self.import_source(self.text), self.source)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)

    def test_same_assertion_different_command_is_idempotent(self):
        self.s.execute(WORKER, command("proposeAssertion", "a", self.payload, key="new-call"))
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 1)

    def test_worker_cannot_publish_or_review(self):
        for action in ["publishSnapshot", "reviewAssertion", "adjudicateDispute", "setSourceVisibility"]:
            self.fails("permission", lambda: self.s.execute(WORKER, command(action, "a", {})))

    def test_anonymous_cannot_mutate(self):
        self.fails("permission", lambda: self.s.execute(PUBLIC, command("proposeAssertion", "b", self.payload)))

    def test_role_injection_not_accepted(self):
        req = command("reviewAssertion", "a", {}, 1)
        req["role"] = "publisher"
        self.fails("invalid_input", lambda: self.s.execute(WORKER, req))

    def test_capability_revocation_blocks_command_replay(self):
        self.review()
        revoked = Principal(HUMAN.id, frozenset(), "human")
        self.fails("permission", lambda: self.review(revoked))

    def test_quotes_offsets_hashes_and_concepts_rejected(self):
        changes = [lambda p: p["evidence"][0].update(quote="Invented"), lambda p: p["evidence"][0].update(start=True),
                   lambda p: p["evidence"][0].update(end=100000), lambda p: p["evidence"][0].update(text_hash="wrong"),
                   lambda p: p.update(object="http://invented/goal")]
        for mutate in changes:
            p = copy.deepcopy(self.payload)
            mutate(p)
            self.fails("invalid_input", lambda: self.s.execute(WORKER, command("proposeAssertion", "bad", p)))

    def test_source_from_another_project_rejected(self):
        p = copy.deepcopy(self.payload)
        p["source_version"] = self.import_source(self.text, "OTHER")
        self.fails("invalid_input", lambda: self.s.execute(WORKER, command("proposeAssertion", "bad", p)))

    def test_authentic_quote_does_not_certify_claim(self):
        p = copy.deepcopy(self.payload)
        p["detail"]["claim"] = "The project cured every disease."
        self.s.execute(WORKER, command("proposeAssertion", "nonsense", p))
        self.assertEqual(self.s.review_state("nonsense", 1), "unreviewed")
        self.fails("quality_gate", lambda: self.publish("reviewed", ["nonsense"]))

    def test_ai_review_not_human_approval(self):
        self.review(AI)
        self.assertEqual(self.s.review_state("a", 1), "ai_reviewed")
        self.fails("quality_gate", lambda: self.publish("reviewed"))

    def test_independent_opinions_preserved_without_assertion_change(self):
        self.review(HUMAN)
        self.review(OTHER, "reject")
        self.assertEqual(len(self.s.opinions("a", 1)), 2)
        self.assertEqual(self.s.head("a", PUBLIC)[0]["revision"], 1)
        self.assertEqual(self.s.review_state("a", 1), "disputed")

    def test_adjudication_compare_and_swap_and_reopening(self):
        self.review(HUMAN)
        self.review(OTHER, "reject")
        ids = sorted(x["id"] for x in self.s.opinions("a", 1))
        p = {"expected_case_revision": 2, "opinion_ids": ids, "outcome": "uphold", "reason": "The proposed activity is supported; no achieved result is claimed."}
        self.s.execute(JUDGE, command("adjudicateDispute", "a", p, 1))
        p["outcome"] = "reject"
        self.fails("version_conflict", lambda: self.s.execute(JUDGE, command("adjudicateDispute", "a", p, 1)))
        self.assertEqual(self.s.review_state("a", 1), "human_approved")
        self.review(Principal("third", frozenset({"review"}), "human"), "request_evidence")
        self.assertEqual(self.s.review_state("a", 1), "disputed")

    def test_proposer_cannot_review_own_assertion(self):
        self.fails("independence", lambda: self.review(Principal("worker", frozenset({"review"}), "model")))

    def test_ai_only_rejection_is_a_dispute_until_a_human_rules(self):
        self.review(AI, "reject")
        self.assertEqual(self.s.review_state("a", 1), "disputed")
        ids = sorted(x["id"] for x in self.s.opinions("a", 1))
        p = {"expected_case_revision": 1, "opinion_ids": ids, "outcome": "reject", "reason": "Source shows no own activity for this target."}
        self.s.execute(JUDGE, command("adjudicateDispute", "a", p, 1))
        self.assertEqual(self.s.review_state("a", 1), "rejected")

    def test_adjudicator_cannot_be_an_opinion_author(self):
        judge_reviewer = Principal("judge-reviewer", frozenset({"review", "adjudicate"}), "human")
        self.review(judge_reviewer, "reject")
        ids = sorted(x["id"] for x in self.s.opinions("a", 1))
        p = {"expected_case_revision": 1, "opinion_ids": ids, "outcome": "reject", "reason": "Ruling on my own opinion."}
        self.fails("independence", lambda: self.s.execute(judge_reviewer, command("adjudicateDispute", "a", p, 1)))

    def test_review_and_adjudication_context_is_recorded_immutably(self):
        params = {"verdict": "reject", "dimension": "activity", "reason": "Blind reference disagrees.", "independence": "blind"}
        seq = self.s.execute(AI, command("reviewAssertion", "a", params, 1))["event_id"]
        ids = sorted(x["id"] for x in self.s.opinions("a", 1))
        p = {"expected_case_revision": 1, "opinion_ids": ids, "outcome": "uphold", "reason": "Activity is stated.", "entered_by": "assistant transcribing the author's ruling"}
        aseq = self.s.execute(JUDGE, command("adjudicateDispute", "a", p, 1))["event_id"]
        ctx = {r["seq"]: json.loads(r["context"]) for r in self.s.db.execute("SELECT * FROM action_context")}
        self.assertEqual(ctx[seq], {"independence": "blind"})
        self.assertEqual(ctx[aseq]["entered_by"], "assistant transcribing the author's ruling")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.db.execute("UPDATE action_context SET context='{}'")
        self.fails("invalid_input", lambda: self.s.execute(HUMAN, command("reviewAssertion", "a", {**params, "independence": "peeked"}, 1)))

    def test_omitted_opinion_blocks_adjudication(self):
        self.review()
        p = {"expected_case_revision": 1, "opinion_ids": [], "outcome": "uphold", "reason": "Ignored opinion"}
        self.fails("stale_dependency", lambda: self.s.execute(JUDGE, command("adjudicateDispute", "a", p, 1)))

    def test_corrected_revision_does_not_inherit_review(self):
        self.review()
        p = copy.deepcopy(self.payload)
        p["detail"]["claim"] = "A corrected, narrower claim."
        self.s.execute(ADMIN, command("correctAssertion", "a", {"replacement": p, "reason": "Corrected scope."}, 1))
        self.assertEqual(self.s.review_state("a", 2), "unreviewed")
        self.assertEqual(self.s.review_state("a", 1), "human_approved")
        self.fails("version_conflict", lambda: self.review(revision=1, key="late"))

    def test_source_update_invalidates_dependent_assertion(self):
        self.review()
        self.import_source(self.text + " Later update.")
        self.assertTrue(self.s.head("a", PUBLIC)[0]["stale"])
        self.fails("stale_dependency", lambda: self.publish())
        self.fails("stale_dependency", lambda: self.review(key="new"))

    def test_failed_publication_preserves_active_pointer(self):
        original = self.publish()["snapshot_id"]
        self.fails("invalid_input", lambda: self.publish(sources=[self.import_source("Another source", "OTHER")]))
        self.assertEqual(self.s.snapshot()["snapshot_id"], original)

    def test_reviewed_publication_rejects_missing_coverage(self):
        self.review()
        findings = [{"source_version": self.source, "processing_state": "not_assessed", "reason": "missing_rubric", "semantic_decision": None}]
        self.fails("quality_gate", lambda: self.publish("reviewed", findings=findings))

    def test_operational_errors_cannot_be_negative_mappings(self):
        findings = [{"source_version": self.source, "processing_state": "processing_error", "reason": "invalid_json", "semantic_decision": "no_mapping"}]
        self.fails("invalid_input", lambda: self.publish(findings=findings))

    def test_publication_contains_exact_identity_and_quote(self):
        self.publish()
        a = self.s.snapshot()["assertions"][0]
        self.assertEqual(a["assertion_id"], "a")
        self.assertEqual(a["payload"], self.payload)
        self.assertEqual(a["review_status"], "unreviewed")

    def test_read_requires_published_snapshot(self):
        self.fails("not_found", lambda: self.s.snapshot())

    def test_revoked_access_filters_sources_assertions_derived_and_findings(self):
        self.review()
        self.publish(findings=[{"source_version": self.source, "processing_state": "not_assessed", "reason": "missing_rubric", "semantic_decision": None}])
        self.s.execute(ADMIN, command("setSourceVisibility", self.source, {"visibility": "private"}))
        snapshot = self.s.snapshot()
        for key in ("sources", "assertions", "derivations", "operational_findings"):
            self.assertFalse(snapshot[key])
        self.fails("permission", lambda: self.s.query(PUBLIC, "a", known_at=2))

    def test_rollback_cannot_restore_restricted_source(self):
        sid = self.publish()["snapshot_id"]
        self.s.execute(ADMIN, command("setSourceVisibility", self.source, {"visibility": "private"}))
        self.fails("permission", lambda: self.s.execute(ADMIN, command("rollbackPublication", sid, {})))

    def test_rollback_cannot_restore_old_review(self):
        self.review()
        sid = self.publish()["snapshot_id"]
        self.review(OTHER, "reject")
        self.fails("stale_dependency", lambda: self.s.execute(ADMIN, command("rollbackPublication", sid, {})))

    def test_rule_keeps_modality_and_unique_counts_with_alternative_proofs(self):
        self.review()
        self.s.execute(WORKER, command("proposeAssertion", "b", self.payload))
        self.review(aid="b")
        derived = self.s.derive_navigation(PUBLIC, ["a", "a", "b"])
        self.assertEqual(len(derived), 1)
        self.assertEqual(len(derived[0]["justifications"]), 2)
        self.assertTrue(all(x["modality"] == "planned" for x in derived[0]["justifications"]))
        self.s.execute(ADMIN, command("invalidateMapping", "a", {"reason": "Withdrawn"}, 1))
        self.assertEqual(len(self.s.derive_navigation(PUBLIC, ["a", "b"])[0]["justifications"]), 1)
        self.s.execute(ADMIN, command("invalidateMapping", "b", {"reason": "Withdrawn"}, 1))
        self.assertFalse(self.s.derive_navigation(PUBLIC, ["a", "b"]))

    def test_open_dispute_withdraws_current_derivation(self):
        self.review()
        self.assertTrue(self.s.derive_navigation(PUBLIC, ["a"]))
        self.review(OTHER, "reject")
        self.assertFalse(self.s.derive_navigation(PUBLIC, ["a"]))

    def test_cycle_rejected(self):
        concepts = copy.deepcopy(CONCEPTS)
        concepts[0]["broader"] = [CONCEPTS[1]["uri"]]
        with self.assertRaises(ContractError):
            MeshStore(":memory:", concepts)

    def test_invalid_taxonomy_does_not_pin_persistent_store(self):
        for kind in ("cycle", "dangling"):
            with self.subTest(kind=kind):
                concepts = copy.deepcopy(CONCEPTS)
                concepts[0]["broader"] = [CONCEPTS[1]["uri"] if kind == "cycle" else "missing-parent"]
                path = Path(self.tmp.name) / f"invalid-{kind}.sqlite"
                self.fails("invalid_input", lambda: MeshStore(path, concepts))
                recovered = MeshStore(path, CONCEPTS)
                try:
                    self.assertEqual(recovered.seq(), 0)
                    result = recovered.execute(ADMIN, command("importSourceVersion", "recovered", {
                        "subject": "recovered", "text": "Synthetic recovery check.",
                        "metadata": {}, "visibility": "public",
                    }))
                    self.assertEqual(result["event_id"], 1)
                finally:
                    recovered.close()

    def test_query_rejects_invalid_or_non_utc_instants(self):
        for value in ("2026-09-10", "2026-09-10T00:30:00", "2026-09-10T00:30:00+01:00", "", "not-a-time", 42, True):
            with self.subTest(value=value):
                self.fails("invalid_input", lambda: self.s.query(PUBLIC, "a", valid_at=value))

    @unittest.skipUnless(hasattr(time, "tzset"), "Host timezone switching requires time.tzset")
    def test_query_utc_instants_are_host_timezone_independent(self):
        p = copy.deepcopy(self.payload)
        p["valid_time"] = {"kind": "interval", "from": "2026-09-10T00:00:00Z", "to": "2026-09-10T01:00:00Z"}
        self.s.execute(ADMIN, command("correctAssertion", "a", {"replacement": p, "reason": "Synthetic UTC interval."}, 1))
        prior_tz = os.environ.get("TZ")
        try:
            for zone in ("UTC0", "EST5"):
                os.environ["TZ"] = zone
                time.tzset()
                with self.subTest(zone=zone):
                    for value in ("2026-09-10T00:30:00Z", "2026-09-10T00:30:00+00:00"):
                        self.assertEqual(self.s.query(PUBLIC, "a", valid_at=value)["temporal_state"], "applicable")
                    self.assertEqual(self.s.query(PUBLIC, "a", valid_at="2026-09-10T01:00:00Z")["temporal_state"], "outside_interval")
                    self.fails("invalid_input", lambda: self.s.query(PUBLIC, "a", valid_at="2026-09-10T00:30:00"))
        finally:
            if prior_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prior_tz
            time.tzset()

    def test_unknown_time_never_becomes_ingestion_time(self):
        self.assertEqual(self.s.query(PUBLIC, "a", valid_at="2026-09-10T00:00:00Z")["temporal_state"], "temporal_unknown")

    def test_two_knowledge_cutoffs_preserve_changed_validity(self):
        p = copy.deepcopy(self.payload)
        p["valid_time"] = {"kind": "interval", "from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}
        first = self.s.execute(ADMIN, command("correctAssertion", "a", {"replacement": p, "reason": "Applicability documented."}, 1))
        p["valid_time"]["to"] = "2026-09-08T00:00:00Z"
        second = self.s.execute(ADMIN, command("correctAssertion", "a", {"replacement": p, "reason": "Retroactive end date correction."}, 2))
        self.assertEqual(self.s.query(PUBLIC, "a", known_at=first["event_id"], valid_at="2026-09-10T00:00:00Z")["temporal_state"], "applicable")
        self.assertEqual(self.s.query(PUBLIC, "a", known_at=second["event_id"], valid_at="2026-09-10T00:00:00Z")["temporal_state"], "outside_interval")

    def test_immutable_records_reject_update_delete(self):
        for table in ("sources", "events", "revisions"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.s.db.execute("DELETE FROM " + table)

    def test_bad_time_correction_rolls_back_every_write(self):
        old_seq = self.s.seq()
        p = copy.deepcopy(self.payload)
        p["valid_time"] = {"kind": "interval", "from": None, "to": None}
        self.fails("invalid_input", lambda: self.s.execute(ADMIN, command("correctAssertion", "a", {"replacement": p, "reason": "Invalid bounds"}, 1)))
        self.assertEqual(self.s.seq(), old_seq)
        self.assertEqual(self.s.head("a", PUBLIC)[0]["revision"], 1)

    def test_other_profile_reuses_review_publication_and_history_without_sdg(self):
        text = json.dumps({"temperature": 24, "unit": "Cel"})
        version = self.import_source(text, "sensor-reading:1")
        p = {"profile": "observation", "subject": "sensor-reading:1", "predicate": "reported_temperature", "object": {"value": 24, "unit": "Cel"},
             "source_version": version, "evidence": [{"kind": "field", "field": "temperature", "value": 24, "text_hash": hashlib.sha256(text.encode()).hexdigest()}],
             "semantic_state": "supported", "modality": "reported_measurement", "valid_time": {"kind": "instant", "at": "2026-09-25T12:00:00Z"}, "detail": {"synthetic": True}}
        self.s.execute(WORKER, command("proposeAssertion", "observation:1", p))
        self.review(aid="observation:1")
        self.publish("reviewed", ["observation:1"], [version])
        self.assertEqual(self.s.snapshot()["assertions"][0]["payload"]["object"]["unit"], "Cel")
        self.assertEqual(self.s.query(PUBLIC, "observation:1", valid_at="2026-09-25T12:00:00Z")["temporal_state"], "applicable")

    def test_two_connections_keep_concurrent_reviews(self):
        def submit(who):
            store = MeshStore(self.path, CONCEPTS)
            try:
                return store.execute(who, command("reviewAssertion", "a", {"verdict": "uphold", "dimension": "scope", "reason": "Independent read."}, 1))
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [HUMAN, OTHER]))
        self.assertEqual(sorted(x["case_revision"] for x in results), [1, 2])
        self.assertEqual(len(self.s.opinions("a", 1)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
