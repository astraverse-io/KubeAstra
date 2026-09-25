"""Eval harness — quality-regression tests for LLM-decision features.

Smoke tests (in `tests/`) prove features RUN. Evals (in `evals/`) prove
features RETURN CORRECT RESULTS. Different cadence, different goals:

  smoke tests  →  run on every commit, must be deterministic + offline
  evals        →  run nightly on main, may hit real LLMs/Qdrant

Per Appendix E, evals are required for features where the LLM makes a
choice that affects user-visible output:
  §1 summarization — does the summary preserve every error signature?
  §8 RAG router — does it fire grounded/cached when it should?
  §9 capture classifier — does worthy=True correlate with human label?

See `evals/_runner.py` for the framework, `evals/router/run.py` for a
working example.
"""
