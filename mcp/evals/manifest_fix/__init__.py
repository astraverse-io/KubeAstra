"""Manifest-fix eval suite (design §7).

A golden set of "fix this Kubernetes manifest" tasks: each case pairs a broken
manifest + symptom with a known-good fix and a set of objective checks. The
:mod:`validators` core scores any candidate fix in [0, 1] with pure functions
(no LLM, no network); the ``run`` module drives the suite offline (self-checks)
and LIVE (agent-produced fixes scored by validators + an LLM judge).
"""
