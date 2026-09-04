# Viewer Impression Deep Reflection v2

## Implementation status

The v2 snapshot and checkpointed pipeline are wired into the existing worker.
New requests use v2 when its explicitly configured roles are available. The
feature remains disabled by default; this work has not been deployed or tested
against a production model. Local implementation, regression, startup and v1
upgrade checks are complete. Real-model literary quality is not certified by
these offline checks. No production configuration was changed.

Implemented:

- A dedicated read-only candidate query over active **and retained archived**
  account conversations, valid topics and account-only episodic memories.
- Account-scoped nickname history and relationship timeline allowlists. Neither
  deleted nickname reconstruction nor relationship score injection is allowed.
- Bounded, diverse retrieval instead of recent-N: historical span anchors,
  importance, recent delta, distinct sessions and topics.
- Frozen v2 snapshot projection with persona hash, previous cutoff only (never
  previous letter text), historical/delta IDs and observed interaction periods.
- Strict internal Dossier, Reflection, Draft and Critic schemas. Invalid IDs are
  rejected; uncited dossier observations are removed; before/after evidence is
  distinct and chronologically ordered. Representative quotations come from
  backend-owned evidence, not model-authored quotations.
- Complete-JSON chronological archaeology chunking, including subdivision of
  oversized prose records. Chunk count is bounded; overflow is explicit, not a
  silent fallback to recent-N or v1.
- Write-once task-stage checkpoints with account, execution-token, live lease
  and memory-preference checks. Retry preserves them; success, permanent failure,
  snapshot clearing and task deletion remove them via SQLite triggers.
- Explicit independent role routing for archaeology, synthesis, writer and
  critic. None falls back to the normal reply model.
- Async acceptance freezes v2 evidence and pipeline policy; old frozen v1 jobs
  may finish after upgrade without turning new v2 jobs into v1.
- Sequential archaeology chunks and a checkpointed binary merge tree feed
  synthesis, writer, critic and at most one writer repair. Completed checkpoints
  survive retries and lease reclamation; validation is repeated on recovery.
- Stage-specific provider windows defer without consuming a retry. Live reply
  gate occupancy or queued/processing SC defers the next stage. Long HTTP calls
  use a separate bounded executor, not the live default executor; cancellation
  retains its slot until the underlying physical HTTP request actually finishes.
- Memory epoch checks close the snapshot-read/clear/re-enable/accept race.
  Opt-out, nickname-history deletion and account deletion invalidate active
  tasks and delete private intermediates and current letters atomically.
- Strict stage JSON rejects duplicate keys and non-finite constants. New-delta
  claims need a newer source, theme dates must fit cited time ranges, and an
  overlapping time aggregate cannot certify a before/after change.
- Final letter checks preserve paragraphs and reject internal terms/IDs and
  report formatting. Ordinary v2 failure logs contain fixed codes, not raw model
  validation errors or private source text.
- Admin stage aggregates cover attempts, transport success, latency, reported
  token usage and priced cost; validation outcomes and checkpoint hits are
  separate counters. Missing usage/pricing remains unknown, not zero.

Final hardening:

- Compute a common stage-output ceiling before archaeology, reserving room for
  merge children and repair artifacts. No Dossier/Reflection JSON is truncated.
- Oversized downstream raw quotations use explicit head/tail windows with
  offsets and omitted-character counts, retaining every requested evidence ID.
  Archaeology still consumes all frozen candidates through bounded chunks.
- Checkpoint reuse requires the exact prompt-input hash, in addition to the
  existing account/token/lease and grounding checks.
- Recheck background priority before each provider fallback, not only at the
  beginning of a logical stage call.
- Publish privacy epoch advancement, ordinary memory removal, episodic cleanup
  and impression cancellation in one transaction. Concurrent readers cannot
  see a new epoch paired with old episodic evidence; failure rolls back the
  whole clear operation.

## Configuration added

All models are explicit. In `AI__PROVIDERS`, use these `models` and `reasoning`
keys independently (role-specific reasoning still requires an enabled provider
reasoning protocol):

```text
viewer_memory_archaeologist
viewer_impression_synthesizer
viewer_impression
viewer_impression_critic
```

Single-provider compatibility exposes `AI__<ROLE>_MODEL` and
`AI__<ROLE>_TIMEOUT`. Archaeology defaults to a 600-second timeout; the other
three roles default to 300 seconds. No real model IDs or credentials are
preconfigured by this change.

For example, a *dedicated* multi-provider entry can use this mapping (replace
all placeholders and merge it into your existing `AI__PROVIDERS` array):

```json
{
  "name": "impression-background",
  "base_url": "https://provider.example/v1",
  "api_key": "replace-me",
  "enabled": true,
  "weight": 10,
  "models": {
    "viewer_memory_archaeologist": "your-large-context-model",
    "viewer_impression_synthesizer": "your-reflection-model",
    "viewer_impression": "your-letter-model",
    "viewer_impression_critic": "your-critic-model"
  },
  "reasoning_protocol": "openai",
  "reasoning": {
    "viewer_memory_archaeologist": "high",
    "viewer_impression_synthesizer": "high",
    "viewer_impression": "high",
    "viewer_impression_critic": "high"
  }
}
```

Use only a reasoning protocol supported by your provider. Explicit model names
may be identical across all four roles. The example makes no external request.

New `VIEWER_IMPRESSION__` settings:

| Setting | Default | Bound |
| --- | ---: | ---: |
| `MAX_FRAGMENT_CANDIDATES` | 500 | 1–2000 |
| `MAX_TOPIC_CANDIDATES` | 100 | 1–2000 |
| `MAX_EPISODIC_CANDIDATES` | 100 | 1–2000 |
| `MAX_NICKNAME_HISTORY` | 50 | 1–500 |
| `ARCHAEOLOGIST_MAX_PROMPT_CHARS` | 600000 | 8000–4000000 |
| `SYNTHESIZER_MAX_PROMPT_CHARS` | 80000 | 8000–500000 |
| `WRITER_MAX_PROMPT_CHARS` | 40000 | 8000–500000 |
| `CRITIC_MAX_PROMPT_CHARS` | 80000 | 8000–500000 |
| `MAX_REPAIR_PASSES` | 1 | 0–1 |
| `MAX_ARCHAEOLOGY_CHUNKS` | 256 | 1–1024 |
| `STAGE_OUTPUT_CHARS` | 8000 | 1000–80000 |
| `ALLOW_V1_FALLBACK` | false | boolean |
| `ALLOW_WITHOUT_CRITIC` | false | boolean |

The budgets are character ceilings, **not** provider token-window guarantees.
Fixed system/schema overhead counts toward each ceiling. Operators must set
them conservatively for every provider allowed to handle that role. The legacy
`MAX_PROMPT_CHARS` remains for v1; it is not the v2 pipeline budget.

`STAGE_OUTPUT_CHARS` is an upper bound, not a required output length. The actual
ceiling can be reduced to fit all configured downstream stages. If even fixed
schemas/artifacts and minimum quotation metadata cannot fit, the task fails
explicitly rather than sending invalid JSON, silently dropping citations or
switching to recent-N retrieval. Quotation windows are explicitly incomplete;
the critic must not infer support from omitted passages.

These settings remain restart-required along with the existing impression
settings. `ALLOW_V1_FALLBACK` only applies when accepting a new request lacking
core v2 roles; it never downgrades an in-flight v2 job. Missing critic may be
bypassed only with explicit `ALLOW_WITHOUT_CRITIC`; a configured critic outside
its active time window causes a defer, not a bypass. Before upgrading an
enabled v1 installation, configure all four roles or explicitly choose the
documented fallback policy; otherwise new generation is unavailable.

## SQLite and privacy

`viewer_impression_deep_reflection_v2` creates
`account_viewer_impression_stages(task_id, stage_key, result_json, created_at)`.
The primary key is `(task_id, stage_key)`. An existing v1 database upgrades at
normal initialization; no manual data conversion is required.

`account_viewer_impression_epochs(account_id, epoch)` records only a monotonic
privacy invalidation counter. SQLite triggers invalidate active jobs when memory
is disabled, a nickname-history row is removed, or the account is deleted. A
snapshot captured before invalidation cannot later be accepted after re-enable.

This table contains private intermediate task state, not durable user memory.
It must never appear in public APIs, live reply prompts or ordinary logs. It
inherits raw-snapshot lifetime, including direct task deletion paths where
SQLite foreign-key enforcement is not enabled. Only completed stage results
are stored; existing retry/lease/cooldown/current-letter transactions remain
authoritative.

Checkpoints include an exact input hash. A changed stage prompt, chunk layout
or source payload cannot reuse an old result solely because its evidence IDs
match. Before a future prompt-changing v2 upgrade, let pending v2 tasks finish
with their original code version; this implementation does not transcode
in-flight checkpoints. Existing frozen v1 tasks can still finish after upgrade
even when new-request `ALLOW_V1_FALLBACK` is false.

## Public and admin contracts

`GET /auth/profile/impression` and `POST /auth/profile/impression/generate`
keep their paths, authentication and asynchronous 202 behavior. Clients never
receive archaeology/synthesis/writer/critic progress. Top-level status remains
`unavailable | empty | ready | processing`.

The only enum extension is `generation.status = failed`: an exhausted latest
attempt remains visible without internal error details. Top-level status is
still `ready` when an older letter exists, or `empty` otherwise. The existing
letter stays readable, and only successful generation starts the normal cooldown.
Active tasks still expose `pending | processing | failed_retryable`. A newer
accepted or completed task replaces the previous failure indication.

`GET /admin/viewer-impression/stats` adds `deep_reflection` with scope
`current_process`. Its `attempts` rows are grouped by stage/role/provider/model;
`validations` separates accepted/rejected/cache-hit counts. Stage aggregates
reset on process restart. Existing durable token audit still groups by AI role;
repair shares the writer role and merge shares the archaeologist role. No raw
evidence, dossier, reflection or letter is added to diagnostic logs.

## Evidence caveats

The first reader does not join optional public session context. It also omits
free-form `resolved_reference` from model inputs until there is an account-safe
reference contract. This avoids importing another account's context.

Interaction periods currently describe **selected retained** fragments. A gap
does not establish real-world absence; the prompt explicitly disallows that
inference. Similarly, citation existence and chronology checks do not prove
semantic entailment. The critic and end-to-end safety validation remain required.
The sensitive-inference lexical guard is conservative, not a comprehensive
semantic safety classifier. A remote HTTP request already sent cannot be
physically recalled on memory revocation; its late result cannot be saved.
Use dedicated provider capacity if upstream shared quotas would otherwise
contend with live replies; local executor isolation does not create additional
upstream quota.

## Offline verification

`tests/replay/test_impression_history_replay.py` seeds the July-plan/August-
development/gap/return/September-launch scenario, including work, Miku, server
and one-off lunch context. It verifies retained archived history, the cutoff,
observed periods, grounded fixture outputs, backend-owned raw excerpts, the
final letter and absence of memory/relationship writeback. Its model outputs
are authored fixtures: passing does **not** prove a real model's literary or
inference quality.

Unit tests cover role absence, window deferral, per-stage provider failure and
abrupt cancellation/reclaim, immutable checkpoints, token mismatch, opt-out
during archaeology/writer, memory clearing during critic, late results and
current-letter preservation. HTTP tests exercise actual response filtering,
202 acceptance without AI, terminal failure and authentication/opt-in boundaries.

Tests use fake providers and temporary SQLite databases. Never run development
tests with the production `.env` or production database as the working directory.

Verification on 2026-09-04 (isolated Python 3.11, no external model calls):

- Full clean-tree regression: **721 unit + 63 contract + 11 replay = 795 passed**.
  This covers realtime replies, persona/emotion/appraisal, relationship/memory,
  selectors, SC, Director/Mainline, moderation and sponsors. Seven initial
  Windows SQLite-handle/timing fixture failures were independently reproduced
  on baseline `64b2d9a`; the affected fixtures now close SQLite handles and
  await their asynchronous work to finish deterministically.
- After the final atomic privacy-clear fix, **4 targeted tests passed**, including
  two new tests for a concurrent WAL reader and rollback on episodic failure.
  The entire suite was not rerun after this final fix.
- Real FastAPI lifespan and HTTP startup: `/status` and `/openapi.json` returned
  200; impression remained off, model calls were zero, shutdown completed.
- Upgrade from a database created by actual v1 code at `64b2d9a`: account, old
  letter and pending frozen snapshot survived automatic migration. The v1 job
  completed using a fake writer with new-request fallback disabled;
  `PRAGMA integrity_check` returned `ok`.

### Requirement-to-evidence map

| Goal requirements | Implementation / verification |
| --- | --- |
| P0–6, P18, P21–23 | Candidate reader, frozen snapshot, historical/delta partition, observed periods; candidate tests and history replay |
| P7–16, P20, P22 | Strict grounded schemas, four stage prompts, backend quotations, single repair and final validator; model/pipeline tests |
| P17, P19 | Full-JSON chunking, binary merge, derived output budgets and explicit raw quote windows; budget/prompt/pipeline tests |
| P1, P23–25 | Token/lease heartbeat, immutable checkpoints, input hashes, epoch/atomic clear; runtime/checkpoint/pipeline/governance tests |
| P26 | No memory writeback or previous-letter evidence; full non-impression table comparison in the history replay |
| P27 | Existing role token audit and current-process stage/validation aggregates; audit/metrics tests |
| P28–31 | Stable async HTTP routes, explicit roles and opt-in fallback policies; auth contracts and role/runtime tests |
| P32–34 | Focused safety/recovery coverage, broad regression and authored July–September replay described above |

Human review with explicitly authorized real models remains necessary to judge
the final letter's naturalness and the critic's semantic accuracy. It is not
part of default tests and was not performed against production.
