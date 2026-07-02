# Remote Diagnostics — Per-Environment Staging Parity Checklist

**Plan reference:** [ANSIBLE_REMOTE_DIAGNOSTICS_PLAN.md §15 Phase 0](ANSIBLE_REMOTE_DIAGNOSTICS_PLAN.md#phase-0--environment-and-staging-parity-gate)

Complete this checklist for each environment (e.g. `qa17`, `stg`, `prod`)
**before** setting `remoteDiagnostics.enabled=true` in that environment's
Helm values or listing its `target_id` in a caller scope's
`allowed_target_ids`. Every item must be signed off by the listed owner in
a per-environment sign-off issue — see the [issue
template](../.github/ISSUE_TEMPLATE/remote_diagnostics_staging_signoff.md).

Do not shortcut this checklist. The backend fails closed at pod startup
when the registry is empty, the known-hosts file is unavailable, or
`AGENT_API_TARGETS_PATH` points to a missing file — but every check below
prevents a subtly-wrong configuration that startup validation cannot catch
(e.g. reachable-but-wrong cluster; correct host but stale host key; broad
egress that would allow lateral movement).

Automatable steps reference
[`scripts/verify_remote_diagnostics_target.sh`](../scripts/verify_remote_diagnostics_target.sh),
which runs the network and host-key checks from inside a debug pod that
mirrors the backend's egress.

---

## Section 1 — Environment inventory (owner: environment team)

- [ ] **1.1** `target_id` chosen and unique across the whole registry.
      Must match `^[a-z0-9][a-z0-9-]{0,62}$`. Reserved / retired IDs must
      not be reused because circuit-breaker state and audit history are
      keyed on `target_id`.
- [ ] **1.2** Control-plane host address and port recorded (typically
      `<addr>:22`, but non-default ports are supported). If different
      from `22`, entry in `remoteDiagnostics.knownHostsEntries` must use
      the OpenSSH `[host]:port` form.
- [ ] **1.3** `k8s-diagnostics` OS account created on the control-plane
      host. Shell restricted; no `sudo`; no interactive login.
- [ ] **1.4** That account has a read-only kubeconfig at a known path
      (typically `/home/k8s-diagnostics/.kube/config`). Confirm via
      `kubectl auth can-i --list` — no verbs other than `get`, `list`,
      `watch` should appear.
- [ ] **1.5** Expected `kube-system` namespace UID captured:
      ```
      kubectl -n kube-system get ns kube-system -o=jsonpath='{.metadata.uid}'
      ```
      Recorded in the environment's `remoteDiagnostics.targets.<id>.expected_kube_system_uid`.
- [ ] **1.6** `agent_target_id: <id>` set in the corresponding Ansible
      inventory `group_vars/<env>/all.yml` (see the Ansible-repo
      staging plan). Optional `agent_diagnostic_scope: ["kubernetes"]`.
- [ ] **1.7** Environment mapped to a caller scope. If a fresh scope is
      needed (e.g. `qa-ansible`, `stg-ansible`), decide the name here and
      pass it to Section 5.

---

## Section 2 — Network path (owner: network team)

- [ ] **2.1** Direct-SSH or bastion-mediated routing decision recorded.
      Bastion routing is required only if direct TCP/22 from the backend
      pod network to the control-plane host is unavailable.
- [ ] **2.2** Backend egress source range recorded. All egress from the
      backend pod comes from this range under the release's NetworkPolicy.
- [ ] **2.3** Firewall exception filed and applied on the destination
      network segment (control-plane host or bastion). Rule scope: only
      the recorded egress source range → target IP:port. **Never
      `0.0.0.0/0`** — the Helm template rejects wildcard CIDRs at render
      time, but the destination firewall must also be scoped.
- [ ] **2.4** DNS resolution from the backend pod verified for the target
      hostname. Run [`scripts/verify_remote_diagnostics_target.sh`](../scripts/verify_remote_diagnostics_target.sh)
      `dns <host>` — the script fails closed if resolution is missing.
- [ ] **2.5** TCP/22 reachability from the backend pod's network confirmed.
      Run [`scripts/verify_remote_diagnostics_target.sh`](../scripts/verify_remote_diagnostics_target.sh)
      `tcp <host> <port>` (defaults to 22).
- [ ] **2.6** If bastion-routed: bastion `/32` recorded in
      `remoteDiagnostics.network.bastionEgress`, and reachability confirmed
      to the **bastion**, not the target. Direct-target egress must remain
      denied.
- [ ] **2.7** Staging cluster uses the same CNI + NetworkPolicy engine
      (Cilium / Calico / GKE / EKS native) as the intended production
      target for this environment group. NetworkPolicy behavior
      differences (e.g. `default deny` semantics) will surface at rollout
      otherwise.

---

## Section 3 — SSH host-key trust (owner: security / cluster owner)

The whole trust story rests on §7.3 of the plan. `ssh-keyscan` is a
**verification** tool, not a source of trust.

- [ ] **3.1** Public host key read directly on the control-plane VM
      through an already-trusted channel (bootstrap console, out-of-band
      management, or the environment's approved provisioning path):
      ```
      cat /etc/ssh/ssh_host_ed25519_key.pub
      ```
      Prefer Ed25519. Record RSA additionally only if Ed25519 is
      unavailable.
- [ ] **3.2** Fingerprint captured on that VM:
      ```
      ssh-keygen -l -E sha256 -f /etc/ssh/ssh_host_ed25519_key.pub
      ```
- [ ] **3.3** Public key and fingerprint delivered through the reviewed
      environment configuration PR (or another approved out-of-band
      channel — Slack / email is NOT sufficient).
- [ ] **3.4** From the backend pod network, `ssh-keyscan -t ed25519 <host>`
      returns the **same** key. Cross-check via
      [`scripts/verify_remote_diagnostics_target.sh`](../scripts/verify_remote_diagnostics_target.sh)
      `hostkey <host> <expected-fingerprint>` — the script exits non-zero
      on mismatch.
- [ ] **3.5** The verified line added to
      `remoteDiagnostics.knownHostsEntries` in the environment's Helm
      values file. Format:
      ```
      <alias> ssh-ed25519 <base64key>
      ```
      or for non-default ports:
      ```
      [<alias>]:<port> ssh-ed25519 <base64key>
      ```
- [ ] **3.6** If bastion-routed, repeat 3.1–3.5 for the bastion host.
      Both the bastion **and** the target host key must be pinned.
- [ ] **3.7** Host-key rotation runbook exists and points at this
      checklist. Rotating a host key without walking §3 means the
      backend will fail closed with `host_key_mismatch` until the new
      key is trusted through the same procedure.

---

## Section 4 — Secrets projection (owner: platform team)

Secret material is **externally managed** — the Helm chart never contains
credentials. See plan §7.2.

- [ ] **4.1** Per-target Secret created out-of-band and named to match
      `remoteDiagnostics.targets.<id>.credentialSecretRef.name`. Exactly
      one of these key sets:
      - `key` (private key contents) plus optional `passphrase`
      - `password`

      Use sealed-secrets / external-secrets-operator / `kubectl create
      secret`. Direct commits of the values file with credentials are
      forbidden.
- [ ] **4.2** Per-scope caller token Secret created and named to match
      `remoteDiagnostics.callerScopes.<name>.tokenSecretRef.name`. Keys:
      - `current` — the active bearer token
      - `previous` — optional, one token behind, retained for the
        rotation grace window

      Token generation: `openssl rand -hex 32`. Rotate independently per
      scope; rotating `qa-ansible` never affects `staging-ansible`.
- [ ] **4.3** Backend ServiceAccount confirmed to lack blanket `get
      secrets` — only the specific projected Secret keys reach the pod.
      Verify with:
      ```
      kubectl auth can-i --as=system:serviceaccount:<ns>:<sa> \
        get secrets -n <ns>
      ```
      Expected output: `no`.
- [ ] **4.4** File permissions on Secret projections verified at 0400
      inside the pod. Rendered mounts always set `defaultMode: 0400` per
      the Phase 6 chart; verify at pod start using:
      ```
      kubectl exec <backend-pod> -- stat -c '%a %n' \
        /var/run/agent-target-credentials/<id>/*
      ```
      Any bit outside `0400` is a runtime fail-closed condition (the SSH
      runner's `_read_secret_text` refuses looser modes).

---

## Section 5 — Caller scope authorization (owner: security)

- [ ] **5.1** Caller scope name recorded in
      `remoteDiagnostics.callerScopes.<name>` with
      `allowed_target_ids: [<this env's target_id>, ...]`.
- [ ] **5.2** Target's `allowed_caller_scopes` includes this scope.
      Both directions must be present — the intersection is what
      `authorize_target` enforces.
- [ ] **5.3** Target's `diagnostic_scopes_allowed: [kubernetes]` (v1
      only supports kubernetes; other values will be rejected at load
      time).
- [ ] **5.4** Legacy `AGENT_API_TOKEN` (if still set on the deployment)
      is confirmed to carry **no** target access. Any live-diagnosis
      caller must use a scoped token.
- [ ] **5.5** Token distribution to the Ansible callback controller
      documented — how the controller obtains `DEVOPS_AGENT_API_TOKEN`,
      how rotation is triggered, and who signs off on rotation.

---

## Section 6 — Chart render validation (owner: agent operator)

- [ ] **6.1** `helm/kubeastra/tests/render.sh` passes locally
      with no changes. This gates the checksum annotation, the volume
      name length, and the ban on wildcard CIDRs.
- [ ] **6.2** `helm template <release> ./helm/kubeastra -f
      <env-values>.yaml` produces a manifest set including exactly:
      - one `-agent-remote-diagnostics` ConfigMap
      - the backend Deployment with `AGENT_API_REMOTE_DIAGNOSTICS_ENABLED=true`
        and one credential mount per target, one token mount per scope
      - the backend NetworkPolicy with the target/bastion egress rules
        rendered under `egress:`

      Manual inspection: `grep -E 'cidr|checksum/agent-remote-diagnostics' <rendered>`.
- [ ] **6.3** No `0.0.0.0/0` or `::/0` appears anywhere in the rendered
      NetworkPolicy egress. The template rejects these at render time —
      this is a "belt and braces" grep.
- [ ] **6.4** Rendered ConfigMap contents round-trip through the Python
      loaders. From a checkout of this repo:
      ```
      cd ui/backend && ./venv/bin/python -c '
      from target_registry import load_registry
      from caller_scopes import load_registry as load_scopes
      # (see tests/render.sh for the full snippet)
      '
      ```

---

## Section 7 — Runtime smoke test (owner: agent operator)

Only start Section 7 after Sections 1–6 are fully signed off.

- [ ] **7.1** Deploy the release with `remoteDiagnostics.enabled=false`
      first. Confirm normal (target-less) backend health via
      `/api/v1/agent/invoke` without a `target` field.
- [ ] **7.2** Apply the per-target and per-scope Secrets to the release
      namespace.
- [ ] **7.3** Flip `remoteDiagnostics.enabled=true` **only** for the
      canary target (one entry under `targets:`, one under
      `callerScopes:`). Do not enable the full registry in one step.
- [ ] **7.4** Watch the backend pod start. Successful startup requires:
      - `agent-remote-diagnostics` ConfigMap present
      - Secret projections mounted at the documented paths
      - No `RuntimeError` about missing targets, missing known_hosts,
        or orphan `AGENT_API_TOKEN_PREVIOUS`
- [ ] **7.5** Run the reachability script once more from inside the live
      backend pod (not a debug pod). This proves the actual runtime
      egress path works, not just a similar one:
      ```
      kubectl exec <backend-pod> -- /scripts/verify_remote_diagnostics_target.sh \
        all <host> <port> <expected-fingerprint>
      ```
- [ ] **7.6** One test `POST /api/v1/agent/invoke` from the Ansible
      canary environment (or a hand-crafted curl using the scoped token)
      returns:
      - `status: completed`
      - `diagnostic_mode: live_cluster` (not `live_cluster_partial`,
        not `error_only`)
      - `connection.verified: true`
      - `connection.kube_system_uid` **matches** the value recorded in
        1.5

      A mismatch here means the SSH host is real but points at the wrong
      cluster — treat as a Section 3 or Section 1 regression, not a
      network issue.
- [ ] **7.7** One negative test — POST with a `target_id` this scope is
      **not** allowed to reach — must return `403 unauthorized`. This
      confirms `authorize_target` runs before the SSH connection is
      opened.
- [ ] **7.8** Metrics scrape confirms:
      - `agent_remote_connection_attempts_total{status="success"}` incremented
      - `agent_remote_diagnostic_mode_total{mode="live_cluster"}` incremented
      - No `agent_remote_circuit_open_total` bump

---

## Section 8 — Rollback plan (owner: agent operator)

Documented before Section 7 begins.

- [ ] **8.1** Fast disable: set `remoteDiagnostics.enabled=false` and
      `helm upgrade`. Deployment rolls; the annotation checksum ensures
      pods restart even if the value files look unchanged from a stale
      cache. Confirm live diagnosis stops (`diagnostic_mode` returns to
      `error_only` on subsequent invocations).
- [ ] **8.2** Slower disable: revoke the caller-scope token by rotating
      only `current`, leaving `previous` unset. Backend keeps other
      scopes working; this specific caller is out immediately.
- [ ] **8.3** Circuit-breaker inspection: after any partial degradation,
      operator knows how to read `agent_remote_circuit_state` per target
      and knows the cooldown is 5 minutes with a single half-open probe.
- [ ] **8.4** Post-incident forensics path documented: which log names,
      which metric queries, and which owner to page. Reference §14 of
      the plan.

---

## Sign-off

Each section above must have an owner comment on the sign-off issue
confirming the checks passed and linking to any auxiliary evidence
(firewall ticket, ssh-keyscan output, kubectl exec logs).

The agent operator marks the issue complete only after **all eight
sections** are signed. `remoteDiagnostics.enabled=true` may not merge
against this environment's values file until the sign-off issue is
closed.

The Ansible team's parallel canary work (see the Ansible-repo delta
todos) may proceed to targeting this environment only after
`diagnostic_mode: live_cluster` is confirmed in 7.6.
