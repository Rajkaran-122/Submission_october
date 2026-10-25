"""Independent evaluation harness package for Ghost Data Still Exists.

The harness is deliberately separate from the answer-generation pipeline:

- ``evaluate.py`` validates the output contract, scores predictions against a
  gold answer and prints/records per-case failure diagnostics;
- ``cases/`` contains team-authored mini-datasets whose ``expected.csv`` files
  are hand-written from ``deletion_rules.csv`` and ``problem_statement.md``.

Run ``python evaluation/evaluate.py --help`` for the CLI, or import
``evaluation.evaluate`` from tests/scripts.
"""
