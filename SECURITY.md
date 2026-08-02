# Security Policy

KubeAstra reads kubeconfig files, executes commands against Kubernetes
clusters, and holds LLM provider credentials. We take reports about it
seriously, and we would much rather hear from you privately than read about it
somewhere else first.

## Reporting a vulnerability

**Email <security@astraverse.dev>.**

Or use GitHub's private reporting, which keeps the whole thread in one place
and lets us credit you automatically:
[Report a vulnerability](https://github.com/astraverse-io/KubeAstra/security/advisories/new).

**Please don't open a public issue for a security problem.** Public issues are
indexed within minutes, and every KubeAstra operator is running it against a
real cluster.

Helpful to include, to whatever extent you have it:

- What an attacker can do with it, and what they need in order to start
- The version, and whether it's server mode or desktop mode
- Reproduction steps, a proof of concept, or a failing test
- Anything you think we'd get wrong on a first read

You don't need a polished writeup. A rough report you send today is worth more
than a careful one you never finish.

## What to expect

KubeAstra is maintained by one person, so these are honest numbers rather than
enterprise ones:

| Stage | Target |
|---|---|
| We acknowledge your report | 3 business days |
| We tell you whether we're treating it as a vulnerability | 10 business days |
| Fix released, for something actively exploitable | As fast as we can, and we'll tell you the date |

If you haven't heard back in a week, assume the mail went astray and ping the
maintainer on GitHub — that's not us ignoring you.

## Supported versions

KubeAstra is pre-1.0 and ships from a single line of development. **Only the
latest release gets security fixes.** There are no backports to older tags; if
you're pinned to one, the fix is to upgrade.

| Version | Supported |
|---|---|
| Latest release | ✅ |
| Anything older | ❌ |

## Scope

### In scope

- Bypassing the desktop-mode boundary in `ui/backend/desktop_security.py` —
  reaching the API from another origin, from off-localhost, or without the
  session token
- Authentication or authorization bypass in server mode, including acting as
  another user or reading another user's investigations
- Leaking secrets — LLM API keys, kubeconfig contents, cluster credentials —
  into logs, error messages, saved investigations, telemetry, or an LLM prompt
- Remote code execution, command injection, or path traversal, including via
  crafted kubectl output, playbook content, or Alertmanager payloads
- Causing KubeAstra to act **beyond the operator's own RBAC**, or to perform a
  mutating action the operator did not approve
- **Prompt injection with real consequences** — content in a cluster (pod
  names, annotations, log lines, event messages) that steers the agent into
  running a mutating command, exfiltrating data, or misreporting what it
  found. This is a genuine attack surface for an agent that reads untrusted
  cluster data, and we want these reports.
- Supply-chain problems in what we publish: the Helm chart, container images,
  the PyPI package, the signed desktop builds, or the update feed

### Not vulnerabilities

These are design decisions, documented and deliberate. Reporting them isn't
wrong — but you'll get an explanation rather than a fix:

- **The agent runs `kubectl` with your credentials.** That's the product. It
  can do what the kubeconfig it was given can do, and no more. Restrict it the
  way you'd restrict any operator: with RBAC.
- **Cluster data is sent to the LLM provider you configured.** Which provider,
  and what reaches it, is the operator's choice. Run Ollama locally if that
  boundary matters to you.
- **Desktop mode listens on `127.0.0.1`.** It's bound to loopback and guarded
  by an origin check and a per-session token. A report needs to show those
  being *bypassed*, not just that a port is open.
- **The `demo/` manifests are deliberately broken.** They exist to produce
  CrashLoopBackOff, failed image pulls and stuck jobs for the agent to
  investigate. Findings there aren't findings.
- Missing hardening headers, or scanner output with no demonstrated impact, on
  an application not intended to be exposed to the public internet.
- Vulnerabilities in a dependency that KubeAstra doesn't reach. Tell us anyway
  if you're unsure — but a version number alone isn't an exploit.

## Safe harbor

If you're acting in good faith to find and report a vulnerability, we won't
pursue or support legal action against you. Please:

- Test only against clusters you own or are authorized to test
- Don't access, modify, or delete data that isn't yours
- Don't degrade service for anyone else
- Give us a reasonable window to fix it before going public

## Disclosure

We do coordinated disclosure. We'll agree a date with you, and default to
**90 days** from the report or the day the fix ships, whichever comes first.

We'll credit you by name in the advisory and the release notes unless you'd
rather we didn't — just say so.

## Something already exposed?

If you believe a KubeAstra release, image, or published artifact has shipped
with a leaked credential in it, treat that as urgent and say so in the subject
line. We'd rather be woken up.
