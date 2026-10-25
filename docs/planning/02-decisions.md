# 02 — Decision log

This document records the **decisions made** in the project, along with their rationale and considered alternatives. IDs (`D##`) are strictly incremented; when a decision is superseded, a new decision is added noting the supersedes relationship (retaining previous entries).
Source: Decisions from `docs/planning/00-design.md` and subsequent packaging/delivery phases.
This file is excluded from `code.zip` (packaging excludes `docs/`).

Format: ID — Decision / Rationale / Alternatives / Consequences.

---

## A. Data and Evidence

### D01 — Use participant-facing files only
- **Status**: accepted (2026-10-25)
- **Decision**: The runtime only reads participant-facing files in `<dataset>/` listed in the problem statement; it never reads organizer-only truth, golden answers, or generator internals.
- **Rationale**: Challenge rules explicitly forbid organizer truth; utilizing hidden organizer data constitutes an invalid shortcut.
- **Alternatives**: Reverse-engineering hidden answers (disallowed); hardcoding case family mappings (brittle and non-generalizable).
- **Consequences**: Accuracy is bound by evidence coverage; uncertainties are transparently disclosed in documentation.

### D02 — Claims are not proof: state must be reconstructed at `audit_at`
- **Status**: accepted (2026-10-25)
- **Decision**: `reported_status`, `reported_at`, and `system_activity.reported_result` are treated merely as unverified claims; true status is determined solely by evidence captured at or before `audit_at` (`captured_at <= audit_at`).
- **Rationale**: The core challenge problem involves workflows marked complete while ghost data remains; sample regression confirms frequent divergence between claims and actual states.
- **Alternatives**: Trusting `reported_status` directly (causes systematic errors); taking majority votes between claims and evidence (lacks principled grounding).
- **Consequences**: Requires a complete evidence parsing layer and point-in-time scenario reconstruction.

### D03 — Evidence captured after `audit_at` is only used for static identity linkage
- **Status**: accepted (2026-10-25)
- **Decision**: Evidence with `captured_at > audit_at` cannot support findings or verification; it may only serve as static corroboration that a resource belongs to a user, and must be disclosed in documentation.
- **Rationale**: Challenge rules require deciding case status using only information prior to `audit_at`; static identity links are time-invariant.
- **Alternatives**: Completely ignoring post-audit evidence (loses identity clues for anonymous records); allowing post-audit evidence to determine status (violates point-in-time rule).
- **Consequences**: Validator and evaluation harness verify that all cited evidence satisfies `captured_at <= audit_at`.

### D04 — Identity resolution: current snapshot + `user_audit_log` historical identifiers
- **Status**: accepted (2026-10-25)
- **Decision**: Index current and historical email, phone, and email_hash via `user_identities.csv` and `user_audit_log.csv`; resolve case -> user uniquely via the request email (which may be historical).
- **Rationale**: All 518 request emails uniquely resolve to a user; historical emails are a major source of ghost data.
- **Alternatives**: Matching current email only (misses orphaned copies associated with historical emails); fuzzy matching on names (leads to collisions).
- **Consequences**: `identity.py` must run before evidence attribution; resolution failures must error explicitly rather than fail silently.

### D05 — Evidence selection: highest `captured_at` where `<= audit_at`
- **Status**: accepted (2026-10-25)
- **Decision**: When multiple observations exist for the same resource, select the one with the latest `captured_at` that is still `<= audit_at`.
- **Rationale**: Best reflects the true state at the moment of audit.
- **Alternatives**: Selecting earliest evidence (misses post-deletion states); taking intersections (cannot explain replica drift).
- **Consequences**: Replicas (failover/read/regional/reporting) with lag are interpreted via master-vs-replica drift.

### D06 — Wave evidence packages for scenario reconstruction
- **Status**: accepted (2026-10-25)
- **Decision**: For the majority wave cases, reconstruct scenarios using the wave evidence package (one per wave, captured around the audit date, covering >= 75% of users in that wave). Special edge cases use their dedicated evidence packages.
- **Rationale**: Verified structural pattern in the dataset.
- **Alternatives**: Matching on individual resources only (wave packages are provided as snapshots).
- **Consequences**: Absence of a user in a snapshot can serve as valid negative evidence.

---

## B. Policy, Planning, and Output

### D07 — `deletion_rules.csv` determines category -> action
- **Status**: accepted (2026-10-25)
- **Decision**: Strictly follow `deletion_rules.csv`: DELETE / REDACT / ANONYMIZE / RETAIN / tombstone-redact. Findings, remediation, and verification are derived from rules and current state, never hardcoded per case.
- **Rationale**: The rule table is the sole authoritative policy specification.
- **Alternatives**: Hardcoding case -> steps (violates generalization); relying on intuition (conflicts with RETAIN/REDACT conditions).
- **Consequences**: Policy layer maps categories to actions; retained records produce `VERIFY_RECORD_PRESENT`.

### D08 — Step ordering: phase -> operation table order -> `system_id` -> `resource_id`
- **Status**: accepted (2026-10-25)
- **Decision**: Strict ordering: FINDING -> REMEDIATION -> VERIFICATION; within each phase, sort by operation table order; tie-break by `system_id` and `resource_id` ascending.
- **Rationale**: Problem statement specifies that array order is significant; all 32 samples match this order.
- **Alternatives**: Emitting in discovery order (loses points); sorting only by `step_no`.
- **Consequences**: Enforced by deterministic sorting prior to serialization; verified by validator.

### D09 — Evidence citation rules
- **Status**: accepted (2026-10-25)
- **Decision**: FINDING steps must cite an evidence ID (`file_id`, `activity_id`, `audit_id`); COMPLETE VERIFICATION steps must cite evidence proving current state; INCOMPLETE REMEDIATION and future VERIFICATION steps use an empty string.
- **Rationale**: Required by problem statement; verified on 32 sample cases.
- **Alternatives**: Citing current evidence on remediation steps (contradicts specification).
- **Consequences**: Step generator handles evidence rules separately for findings vs. future remediation/verification.

### D10 — Deterministic core with optional advisory LLM intent check
- **Status**: accepted (2026-10-25)
- **Decision**: Parsing, identity resolution, state reconstruction, policy, planning, and serialization are 100% deterministic. The LLM is an optional advisory check for multilingual deletion intent (temperature 0, cached responses, usage tracked). Default is `--no-llm`.
- **Rationale**: Grading prioritizes factual precision and determinism; LLMs add latency and non-determinism, beneficial only for edge-case semantic intent review.
- **Alternatives**: LLM-generated steps (fragile, expensive, non-reproducible); no model at all (rule-based handles 100% of cases, but optional LLM review adds robustness).
- **Consequences**: System runs fully offline without an API key; usage is recorded in `evaluation/usage_report.md`.

### D11 — LLM response caching for reproducibility
- **Status**: accepted (2026-10-25)
- **Decision**: Responses from `--llm-intent` are saved to `evaluation/intent_cache.json`; identical requests reuse cached responses without network calls.
- **Rationale**: Cost control and reproducible runs.
- **Alternatives**: No caching (results can drift; duplicate costs); storing cache in untracked directory (breaks reproducibility).
- **Consequences**: Cache schema contains only request hash and classification; packaged in `code.zip`.

### D12 — OCR: macOS Vision with fallback
- **Status**: accepted (2026-10-25)
- **Decision**: 11 PNG screenshots OCR'd via `scripts/ocr_images.swift` (macOS Vision). When Swift/macOS is unavailable, `--skip-images` allows skipping image observations without crashing; Linux CI skips OCR tests.
- **Rationale**: Native toolchain is high quality; graders may run on varied platforms.
- **Alternatives**: Bundling third-party OCR neural net (heavyweight, license complexity); ignoring images entirely (misses multimodal evidence).
- **Consequences**: Documented in README; OCR cache not packaged.

---

## C. Packaging and Delivery

### D13 — `code.zip` packaging inventory
- **Status**: accepted (2026-10-25)
- **Decision**: Package `README.md`, `INTERVIEW_GUIDE.md`, `pyproject.toml`, `uv.lock`, `.python-version`, `src/**`, `tests/**`, `evaluation/**` (with reports), `scripts/**`. Generated by `scripts/build_code_zip.py`.
- **Rationale**: Delivers a runnable solution matching the repository root layout.
- **Alternatives**: Moving code into `code/` subdirectory (unnecessary disruption to `src/` layout); omitting tests (reduces verifiability).
- **Consequences**: Clean reproduction via `uv sync` and `uv run python -m ghost_audit run ...`.

### D14 — `code.zip` exclusion rules (hard constraints)
- **Status**: accepted (2026-10-25)
- **Decision**: Exclude `dataset/`, `docs/`, `submission/`, `work/`, `.venv`, `.git`, `.uv-cache`, `.ocr-cache`, `__pycache__`, `.pytest_cache`, `log.txt`, `output.csv`, `.env`, credentials, and nested archives.
- **Rationale**: Challenge rules strictly forbid datasets, virtualenvs, caches, nested archives, and API keys.
- **Alternatives**: Manual `zip -x` (error-prone); packaging docs (risks leaking reference repos).
- **Consequences**: Enforced by assertions in `build_code_zip.py` and checked by `validate_submission.py`.

### D15 — Deterministic packaging
- **Status**: accepted (2026-10-25)
- **Decision**: Zip entries sorted by path; fixed timestamps `1980-01-01T00:00:00`, fixed permissions `0644`, compression level 9; built twice and byte-compared.
- **Rationale**: Guarantees identical archives from identical source trees.
- **Alternatives**: Relying on system zip utilities.
- **Consequences**: Clean sha256 checksums verifiable across environments.

### D16 — `submission/` deliverable count
- **Status**: accepted (2026-10-25; updated by D26)
- **Decision**: `submission/` contains only required deliverables: `output.csv`, `code.zip`, `log.txt`, `INTERVIEW_GUIDE.md`.
- **Rationale**: Adheres strictly to challenge submission specifications.
- **Alternatives**: Copying scratch documents into submission.
- **Consequences**: `validate_submission.py` enforces exact deliverable counts.

### D17 — Standalone stdlib-only validation script
- **Status**: accepted (2026-10-25)
- **Decision**: `scripts/validate_submission.py` uses only Python standard library; returns exit code 0 on success, 1 on validation error, 2 on missing artifacts.
- **Rationale**: Enables pre-submission checks independent of virtualenv.
- **Alternatives**: Relying solely on pytest.
- **Consequences**: Comprehensive automated gate before submission.

### D18 — English documentation standard
- **Status**: accepted (2026-10-25; updated by D26)
- **Decision**: All deliverables, documentation, guides, and internal planning records are maintained in English.
- **Rationale**: Universal accessibility for evaluators and pair programming continuity.
- **Alternatives**: Maintaining bilingual docs (creates synchronization overhead).
- **Consequences**: Zero non-English artifacts in the repository.

### D19 — Package naming: `ghost-audit`
- **Status**: accepted (2026-10-25)
- **Decision**: Package named `ghost-audit` with module `ghost_audit`.
- **Rationale**: Supports standard invocation `uv run python -m ghost_audit`.
- **Alternatives**: Generic template names.
- **Consequences**: Clean imports across tests and CLI.

### D20 — Packaging scripts included in code archive
- **Status**: accepted (2026-10-25)
- **Decision**: Include `scripts/build_code_zip.py` and `scripts/validate_submission.py` in `code.zip`.
- **Rationale**: Enables reviewers to re-verify packaging and contracts.
- **Alternatives**: Omitting scripts.
- **Consequences**: Reviewers have complete transparency into artifact generation.

### D21 — Canonical numeric case ordering
- **Status**: accepted (2026-10-25)
- **Decision**: Rows in `output.csv` must be sorted in ascending numeric case ID order (`case_33, case_99, case_100, ..., case_550`). Lexicographical sorting is flagged as an error.
- **Rationale**: Canonical benchmark evaluation uses numeric ordering.
- **Alternatives**: Python default `sorted()` (alphabetical order places `case_100` before `case_33`).
- **Consequences**: Enforced in `output.py` and `validate_submission.py`.

### D22 — Package intent cache, exclude telemetry
- **Status**: accepted (2026-10-25)
- **Decision**: Include `evaluation/intent_cache.json` in `code.zip`; exclude local run telemetry `evaluation/llm_usage.jsonl`.
- **Rationale**: Cache enables offline reproduction; raw run logs are local execution traces.
- **Alternatives**: Excluding cache (forces re-invocation of LLM).
- **Consequences**: Packaged archive can reproduce cached runs offline.

### D23 — Bilingual interview guide (Superseded by D26)
- **Status**: superseded by D26 (2026-10-25)
- **Decision**: Formerly added `INTERVIEW_GUIDE_CN.md` as a 5th deliverable.
- **Rationale**: Requested by user at the time.
- **Consequences**: Superseded by D26 per user instruction to standardize on English and remove non-English versions.

### D24 — Raw conversation export exclusion
- **Status**: accepted (2026-10-25)
- **Decision**: Machine-specific `.conversations/` raw session exports are gitignored and excluded from `code.zip`. The chat transcript is compiled into `submission/log.txt` via `scripts/build_transcript.py`.
- **Rationale**: Raw exports contain verbose internal artifacts and machine data not suitable for repository distribution.
- **Alternatives**: Committing raw exports directly.
- **Consequences**: Clean repository state.

### D25 — Transcript summarization and sanitization rules
- **Status**: accepted (2026-10-25)
- **Decision**: Compiled transcripts retain verbatim root `log.txt` as PART I; PART II appends sanitized conversation history (omitting internal reasoning blocks, raw tool dumps, and system reminders; masking credential patterns).
- **Rationale**: Produces a readable, auditable, and safe submission transcript.
- **Alternatives**: Dumping unfiltered session logs.
- **Consequences**: Transcript passes security and readability audits.

### D26 — English Standardization and Removal of Non-English Deliverables
- **Status**: accepted (2026-10-25)
- **Decision**: Supersedes D23 and updates D16 and D18. Remove all non-English text across the repository; remove `submission/INTERVIEW_GUIDE_CN.md` as the canonical English guide `INTERVIEW_GUIDE.md` exists; translate all planning documents (`00-design.md`, `01-tasklog.md`, `02-decisions.md`, `03-submission-checklist.md`) and local rules (`AGENTS.md`) into pure English; update `validate_submission.py` to enforce exactly 4 deliverables (`output.csv`, `code.zip`, `log.txt`, `INTERVIEW_GUIDE.md`); update `build_code_zip.py` to remove redundant guides from `ROOT_FILES`.
- **Rationale**: User explicitly instructed to standardize all documentation and artifacts on English, remove files with existing English versions, translate missing ones, and remove any regional terms.
- **Alternatives**: Retaining bilingual documentation (explicitly rejected by user).
- **Consequences**: Entire codebase and submission package are 100% English, fully compliant with international review standards and challenge guidelines.

---

## Open Inquiries and Tracking

| # | Topic | Status | Resolution |
| --- | --- | --- | --- |
| O1 | Hidden canonical ground truth | Unverified | Contract correctness and sample regression are primary targets |
| O2 | Anonymous analytics attribution | Unverified | Documented as an approximation in evaluation report |
| O3 | Evaluation scoring weights | Unverified | Optimize across all specified dimensions |
| O4 | Official deadline configuration | Unset | Maintained as unconfigured per upstream AGENTS |
| O5 | LLM intent review impact | Verified | LLM serves strictly as advisory check; no deterministic overrides |
| O6 | Final empirical metrics | Verified | Empirical token counts and costs populated in usage report |
