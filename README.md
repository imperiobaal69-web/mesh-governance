# Mesh Governance

[![Tests](https://github.com/imperiobaal69-web/mesh-governance/actions/workflows/tests.yml/badge.svg)](https://github.com/imperiobaal69-web/mesh-governance/actions/workflows/tests.yml)

A Python + SQLite evidence-governance library extracted from **Nexus / Mesh**. It preserves source versions and assertion history, records independent reviews, and controls which revisions can enter a published snapshot.

**39 acceptance tests · Python standard library · Synthetic fixtures · No API keys**

## Run the tests

Requires Python 3.10+ with SQLite support. No package installation is needed.

```sh
git clone https://github.com/imperiobaal69-web/mesh-governance.git
cd mesh-governance
python3 -B -m unittest discover -s scripts -p test_mesh_ontology.py -v
```

Tests use temporary databases and synthetic source text, then clean up after themselves. A successful run reports `Ran 39 tests` and `OK`.

## The problem

A system can store a claim while losing the information needed to judge it: the exact source version, the cited passage, who proposed it, which revision was reviewed, and whether later evidence made it stale.

This library makes those dependencies explicit:

```text
Source version → Assertion revision → Independent review → Published snapshot
       ↓
Source update → Dependent assertions become stale → Reassessment required
```

## Engineering decisions

| Concern | Implementation |
| --- | --- |
| Repeated or conflicting requests | Actor-scoped idempotency keys and request fingerprints inside a SQLite transaction. |
| History preservation | Append-only source, revision and review records, with database triggers rejecting updates and deletes. |
| Evidence consistency | Exact source spans and hashes; assertions must reference the correct subject and current source version. |
| Independent review | The proposer cannot review their own revision; an adjudicator cannot rule on their own opinion. |
| Changing knowledge | Source updates invalidate dependent assertions. Queries distinguish knowledge cutoffs from validity time. |
| Publication | Reviewed publication requires human approval of the exact revision. Exploratory snapshots retain their review status. |
| Rollback | Historical publication cannot be restored when its evidence is restricted, its assertions are stale, or its reviews changed. |

## Start reading here

- **[Implementation](scripts/mesh_ontology.py):** `MeshStore.execute` for transactions and idempotency; `validate` for evidence checks; the review, publication and rollback methods for governance.
- **[Acceptance tests](scripts/test_mesh_ontology.py):** `StoreTests.setUp` and its helpers show a source moving through the command interface. Tests cover concurrent reviews, role injection, stale evidence, disputed decisions, temporal corrections and rollback.
- **[Provenance](PROVENANCE.json):** source commit and SHA-256 hashes for both unchanged Python files.

## Scope and limitations

This is a local SQLite library. Trusted host code must supply `Principal` identities and capabilities; the module does not authenticate remote users. Do not attach it directly to an unauthenticated mutation endpoint.

Matching a quotation and its hash establishes source consistency, not the truth of an interpretation. The tests explicitly demonstrate that an authentic quote can accompany an unsupported claim. Review is a separate operation, and an AI review does not count as human approval.

The sample runs without a model. It does not include the full Nexus interface, research pipeline, datasets, deployment configuration or original repository history. The tests establish the checked local behaviors, not distributed-database scalability or real-world classification accuracy.

## Project context

**Paulo Villalobos — Software Developer | Systems Design & AI**

My work on Nexus / Mesh covers system design, architectural and implementation decisions, and evaluation. Development uses AI coding assistance. This extract demonstrates the system's evidence, revision and review rules through runnable code and tests.

[GitHub profile](https://github.com/imperiobaal69-web)
