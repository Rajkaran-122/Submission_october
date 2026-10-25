"""Command line entry point.

Usage
-----

Run the complete pipeline on the official dataset::

    uv run python -m ghost_audit run \\
        --dataset docs/refs/hackerrank-orchestrate-october26-main/dataset \\
        --output output.csv

Run the labelled sample requests instead (development regression)::

    uv run python -m ghost_audit run --dataset <DIR> --sample --output sample_predictions.csv

The pipeline is deterministic. Optional flags:

``--no-llm`` (default)
    Deterministic multilingual intent classification only; works offline.
``--llm-intent``
    Ask a DeepSeek model to review the intent decision. Responses are cached in
    ``evaluation/intent_cache.json`` so later runs are reproducible and offline.
``--skip-images``
    Skip OCR of PNG evidence (documented accuracy impact).
``--emit-blueprint``
    Write the reconstructed scenario for every case as JSON (debugging aid).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .activity import ActivityIndex
from .dataset import Dataset
from .evidence import parse_all
from .identity import IdentityIndex
from .intent import IntentCache, classify_request_text
from .output import self_check, write_output
from .planner import plan_case
from .scenario import ScenarioBuilder

DEFAULT_DATASET = "docs/refs/hackerrank-orchestrate-october26-main/dataset"
DEFAULT_INTENT_CACHE = "evaluation/intent_cache.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ghost_audit", description="Ghost Data Still Exists deletion audit")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the audit pipeline")
    run.add_argument("--dataset", default=DEFAULT_DATASET, help="path to the participant-facing dataset directory")
    run.add_argument("--requests", default=None, help="requests CSV (default: dataset/deletion_requests.csv)")
    run.add_argument("--sample", action="store_true", help="use dataset/sample_deletion_requests.csv")
    run.add_argument("--output", default="output.csv", help="output CSV path")
    run.add_argument("--skip-images", action="store_true", help="skip OCR of PNG evidence")
    run.add_argument("--no-llm", action="store_true", help="force deterministic intent only (wins over --llm-intent)")
    run.add_argument("--llm-intent", action="store_true", help="review low-signal intent decisions with a DeepSeek model (cached, budgeted)")
    run.add_argument("--llm-budget", type=int, default=40, help="maximum new LLM intent reviews per run (default 40)")
    run.add_argument("--intent-cache", default=DEFAULT_INTENT_CACHE, help="LLM intent response cache path")
    run.add_argument("--emit-blueprint", default=None, help="write reconstructed scenarios as JSON")
    run.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser


def _log(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def _review_candidates(requests):
    """Cases where a semantic review adds information.

    The deterministic classifier is authoritative for explicit non-deletion
    instructions and bilingual deletion vocabulary. The LLM review is aimed at
    the remaining low-signal decisions (no explicit deletion vocabulary matched
    either way) and at confirming the explicit non-deletion refusals, so the
    model budget is spent where the semantic question is genuinely open.
    """

    targets = []
    for request in requests:
        intent = classify_request_text(request.request_text)
        if intent.label == "NON_DELETION" or not intent.matched:
            targets.append(request)
    return targets


def _review_intents(requests, cache_path: Path, quiet: bool, budget: int) -> dict[str, str]:
    """Bounded LLM intent cross-check; the deterministic decision stays authoritative.

    Every call and its token usage is measured (``evaluation/llm_usage.jsonl``)
    and cached by content hash (``evaluation/intent_cache.json``), so the run is
    reproducible offline. The deterministic classifier is never overridden by
    the model; this layer exists to cross-check the semantic reading and to make
    the AI-assisted review explicit and measurable.
    """

    from .llm import DeepSeekClient  # local import: optional dependency on credentials

    try:
        client = DeepSeekClient()
    except RuntimeError as exc:
        _log(quiet, f"LLM intent review disabled: {exc}")
        return {}
    cache = IntentCache(cache_path)
    candidates = _review_candidates(requests)
    labels: dict[str, str] = {}
    new_calls = 0
    cached_calls = 0
    for request in candidates:
        cached = cache.get(request.request_text, client.model)
        if cached:
            labels[request.case_id] = cached
            cached_calls += 1
            continue
        if new_calls >= budget:
            continue
        label = client.classify_intent(request.request_text)
        cache.put(request.request_text, client.model, label, "llm review")
        labels[request.case_id] = label
        new_calls += 1
    _log(
        quiet,
        f"LLM intent review: {len(candidates)} candidate cases, {new_calls} new calls, {cached_calls} cached, budget {budget}",
    )
    return labels


def _read_requests(path: Path):
    from .dataset import Request

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [Request(**row) for row in csv.DictReader(handle)]


def run(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset)
    dataset = Dataset(dataset_dir)
    if args.sample:
        requests = dataset.sample_requests
    elif args.requests:
        requests = _read_requests(Path(args.requests))
    else:
        requests = dataset.deletion_requests
    _log(args.quiet, f"cases: {len(requests)} from {dataset_dir}")

    identities = IdentityIndex(dataset)
    identities.case_to_user(dataset.sample_requests + dataset.deletion_requests + requests)
    activities = ActivityIndex(dataset)
    _log(args.quiet, "parsing evidence ...")
    documents = dataset.load_evidence_documents(skip_images=args.skip_images)
    parsed = parse_all(documents)
    _log(args.quiet, f"evidence files: {len(documents)}, observations: {len(parsed.observations)}")
    warnings = {file_id: items for file_id, items in parsed.warnings.items() if items}
    if warnings:
        _log(args.quiet, f"parser warnings on {len(warnings)} files")

    _log(args.quiet, "reconstructing scenarios ...")
    builder = ScenarioBuilder(
        dataset,
        identities,
        activities,
        parsed.observations,
        parsed.coverage,
        parsed.file_families,
    )
    scenarios = builder.build_all(dataset.sample_requests + dataset.deletion_requests)

    llm_labels: dict[str, str] = {}
    if args.llm_intent and not args.no_llm:
        llm_labels = _review_intents(requests, Path(args.intent_cache), args.quiet, args.llm_budget)

    plans = []
    review_notes: list[str] = []
    for request in requests:
        scenario = scenarios[request.case_id]
        intent = classify_request_text(request.request_text)
        if request.case_id in llm_labels and llm_labels[request.case_id] == "NON_DELETION" and intent.authorizes_deletion:
            # Deterministic classifier found deletion vocabulary; only an
            # explicit non-deletion instruction may override it (already handled
            # by the deterministic classifier), so record the disagreement.
            review_notes.append(f"{request.case_id}: LLM said NON_DELETION, deterministically DELETION")
        plan = plan_case(
            request.case_id,
            scenario.resources,
            intent_authorizes_deletion=intent.authorizes_deletion,
            notes=scenario.notes,
        )
        plans.append(plan)

    write_output(args.output, plans)
    problems = self_check(args.output, [request.case_id for request in requests])
    if problems:
        for problem in problems:
            print(f"CONTRACT ERROR: {problem}", file=sys.stderr)
        return 1
    _log(args.quiet, f"wrote {len(plans)} rows to {args.output}")
    agreement = len(llm_labels) - len(review_notes)
    if llm_labels:
        _log(args.quiet, f"LLM intent review: {agreement}/{len(llm_labels)} agree with the deterministic decision")
    if review_notes:
        _log(args.quiet, f"LLM disagreements recorded: {len(review_notes)}")

    if args.emit_blueprint:
        blueprint = {
            case_id: {
                "user_id": scenario.user_id,
                "resources": [state.__dict__ for state in scenario.resources],
                "notes": scenario.notes,
            }
            for case_id, scenario in scenarios.items()
        }
        Path(args.emit_blueprint).write_text(json.dumps(blueprint, indent=1, ensure_ascii=False), encoding="utf-8")
        _log(args.quiet, f"wrote scenario blueprint to {args.emit_blueprint}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
