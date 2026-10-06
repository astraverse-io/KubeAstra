"""Quality-regression evals for KubeAstra (design §7).

Each sub-package is a golden suite with its own ``run`` module, invoked from the
``mcp`` working dir as ``python -m evals.<name>.run`` (see .github/workflows/evals.yml).
Offline runs are deterministic and free; LIVE runs (EVAL_LIVE_LLM=true) call real
models.
"""
