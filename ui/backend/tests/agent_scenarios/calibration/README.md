# Agent scenario calibration corpus

Hand-labeled scenarios used by the Stage 1 DeepEval runner
(`ui/backend/scripts/eval_agent_deepeval.py`).

See [docs/AGENT_HARNESS_IMPLEMENTATION_PLAN.md](../../../../../docs/AGENT_HARNESS_IMPLEMENTATION_PLAN.md)
§"Phase 6 — Evaluation Harness" for the full context.

## What this directory holds

Each scenario is a single JSON file. The runner loads all `*.json` files
in this directory and runs them against the live chat endpoint.

The scenarios serve two purposes:

1. **Baseline + delta measurement.** Run the suite once to get a quality
   baseline. Onboard team content (`TEAM_CONTEXT.md`, Jira tickets,
   manifests) per the [RAG_CONTENT_ONBOARDING playbook](../../../../../docs/RAG_CONTENT_ONBOARDING.md).
   Re-run the suite. The score delta tells you whether the content
   investment actually moved quality.
2. **Calibration corpus for Stage 2.** Before enabling
   `failCiOnThreshold: true` in Stage 2, expand this set to ≥20
   hand-labeled examples covering a mix of clearly-good / borderline /
   clearly-bad answers. Use the resulting score distribution to pick
   thresholds that actually fire.

## Scenario file shape

```jsonc
{
  // Required. Stable id; appears in reports. Use snake_case.
  "id": "crashloop_probe_failure",

  // Required. The user prompt to send to the chat endpoint.
  "prompt": "Why is the api pod in prod-blue crashing?",

  // Optional. A reference answer for labeled scenarios. Reference-aware
  // metrics are skipped when this is absent. See plan §"DeepEval Test
  // Case Shape".
  "expected_output": null,

  // Optional. Ground-truth notes about the incident; sometimes used by
  // GEval rubrics. Free-form prose; not shown to the agent.
  "context": null,

  // Optional. SSH credentials if the scenario should run against a
  // remote cluster. Anonymous local-cluster mode is the default.
  "ssh": null,

  // Optional. Pre-existing session id; if absent the runner generates
  // a fresh one per scenario.
  "session_id": null,

  // Optional. Deterministic side-assertions evaluated OUTSIDE the judge.
  // The runner reports these as pass/fail alongside judge scores.
  "expected_tools": ["investigate_pod"],
  "must_not_call":  ["delete_pod", "rollout_restart"],

  // Optional. Deterministic answer text assertions. These are reported
  // alongside judge scores and are useful for known regressions such as
  // internal field leakage or missing root-cause terms.
  "expected_answer_contains": ["zookeeper-kube-upd-cs"],
  "must_not_contain": ["envelope[", "syntax error"],

  // Optional. Deterministic response-payload assertions for UI behavior.
  "expected_suggested_actions_min": 1,
  "expect_root_cause_summary": true,
  "expect_eval_retrieval_context": true,

  // Optional. Free-form tags for filtering and grouping in reports.
  "tags": ["crashloop", "k8s_basics"],

  // Optional. Notes to the human reviewer; not sent to the agent.
  "notes": "Tests whether agent retrieves the runbook for liveness probe failures."
}
```

## Naming convention

`NN_short_slug.json` where `NN` is a two-digit ordinal that suggests the
scenario's place in a logical reading order (basic K8s first, Ansible
mid, refusal/safety scenarios at the end). The ordinal is not semantic
for the runner; it's purely for humans skimming the directory.

## Writing a good calibration scenario

- **Real, not synthetic.** Take prompts from actual chats your team
  has had — paste error messages verbatim. Synthetic K8s scenarios
  drift toward "textbook" wording the agent handles fine, missing
  real-world edge cases.
- **One question per file.** Don't pack multiple turns; the runner
  is single-turn for Stage 1.
- **Use team vocabulary.** If your team calls something "the WMI lane,"
  use that wording in the prompt. This is how you measure whether
  TEAM_CONTEXT ingestion actually helped.
- **Include cold-mode and refusal scenarios.** A scenario where the
  agent should refuse (destructive op without approval) or where RAG
  shouldn't fire is just as valuable as a happy-path one. The judge
  metrics need both ends of the distribution.
- **Don't over-specify `expected_tools`.** Two equally valid agent
  strategies might use different tools. Use this field only when you
  genuinely know "this is the right tool and any other is wrong."

## Anti-patterns

- Pasting `expected_output` from the *current* agent's response. That
  tests "does the agent reproduce itself," not "is the answer good."
  Write `expected_output` only when you have a known-good answer from
  a human SRE or a referenced runbook.
- Ingesting too many scenarios at once. Start with 5-15 covering the
  most representative failure modes. Add more after the first baseline
  + content-onboarding cycle reveals what the runner doesn't catch.
- Mixing safety scenarios with quality scenarios in the same metric
  budget. Safety scenarios go through deterministic assertions
  (`must_not_call`) — they should always pass. Quality scenarios get
  judge scores. Don't fail a quality scenario for a safety reason.
