#!/usr/bin/env bash
# Phase 6 render tests — run from repo root:
#   ./helm/kubeastra/tests/render.sh
#
# Exercises:
#   1. Baseline (remoteDiagnostics.enabled=false) — the feature must be inert.
#   2. Direct-SSH scenario — ConfigMap, Deployment mounts, NetworkPolicy egress.
#   3. Bastion scenario — bastion /32 egress only.
#   4. Negative: unrestricted CIDR must fail.
#   5. Negative: empty targets / empty knownHostsEntries must fail.
#   6. Negative: missing credentialSecretRef must fail with a readable message.
set -euo pipefail

CHART_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null && pwd)"
cd "${CHART_DIR}"

require_ok() {
  local label="$1"; shift
  if ! helm template test . "$@" > /dev/null 2>&1; then
    echo "FAIL: ${label} should have rendered cleanly"; exit 1
  fi
  echo "OK: ${label}"
}

require_fail() {
  local label="$1"; local needle="$2"; shift 2
  local output
  if output=$(helm template test . "$@" 2>&1); then
    echo "FAIL: ${label} should have errored"; exit 1
  fi
  if ! grep -q -- "${needle}" <<<"${output}"; then
    echo "FAIL: ${label} error did not include ${needle!r}"; echo "${output}"; exit 1
  fi
  echo "OK: ${label} (rejected as expected)"
}

# 1. Baseline
require_ok "baseline disabled"

# 2. Direct SSH
require_ok "direct SSH" -f tests/values-remote-direct-ssh.yaml

# 3. Bastion
require_ok "bastion" -f tests/values-remote-bastion.yaml

# 4-6. Negative cases — use inline overrides so the fixtures stay valid.
BAD_CIDR=$(mktemp); trap 'rm -f "${BAD_CIDR}"' EXIT
cat > "${BAD_CIDR}" <<EOF
remoteDiagnostics:
  enabled: true
  targets:
    qa17:
      enabled: true
      environment_group: qa
      connection: {type: ssh, host: 10.40.17.10, port: 22, username: k8s-diagnostics, known_hosts_alias: qa17-control-plane}
      expected_kube_system_uid: 1c43dff0-0000-0000-0000-000000000001
      diagnostic_scopes_allowed: [kubernetes]
      allowed_caller_scopes: [qa-ansible]
      credentialSecretRef: {name: agent-target-qa17-credentials}
  callerScopes:
    qa-ansible:
      allowed_target_ids: [qa17]
      tokenSecretRef: {name: agent-caller-token-qa-ansible}
  knownHostsEntries: ["qa17-control-plane ssh-ed25519 AAAA"]
  network:
    targetEgress:
      - {cidr: 0.0.0.0/0, ports: [22]}
EOF
require_fail "reject unrestricted CIDR" "unrestricted CIDR" -f "${BAD_CIDR}"

EMPTY_TARGETS=$(mktemp)
cat > "${EMPTY_TARGETS}" <<EOF
remoteDiagnostics:
  enabled: true
  targets: {}
  callerScopes:
    qa-ansible:
      allowed_target_ids: [qa17]
      tokenSecretRef: {name: agent-caller-token-qa-ansible}
  knownHostsEntries: ["qa17-control-plane ssh-ed25519 AAAA"]
EOF
require_fail "reject empty targets" "remoteDiagnostics.targets is empty" -f "${EMPTY_TARGETS}"
rm -f "${EMPTY_TARGETS}"

NO_CRED=$(mktemp)
cat > "${NO_CRED}" <<EOF
remoteDiagnostics:
  enabled: true
  targets:
    qa17:
      enabled: true
      environment_group: qa
      connection: {type: ssh, host: 10.40.17.10, port: 22, username: k8s-diagnostics, known_hosts_alias: qa17-control-plane}
      expected_kube_system_uid: 1c43dff0-0000-0000-0000-000000000001
      diagnostic_scopes_allowed: [kubernetes]
      allowed_caller_scopes: [qa-ansible]
  callerScopes:
    qa-ansible:
      allowed_target_ids: [qa17]
      tokenSecretRef: {name: agent-caller-token-qa-ansible}
  knownHostsEntries: ["qa17-control-plane ssh-ed25519 AAAA"]
EOF
require_fail "reject missing credentialSecretRef" "credentialSecretRef.name is required" -f "${NO_CRED}"
rm -f "${NO_CRED}"

# 7. Length overrun — 63-char target_id would produce an 82-char volume name.
LONG_ID=$(printf 'q%.0s' {1..63})
LONG_TARGET=$(mktemp)
cat > "${LONG_TARGET}" <<EOF
remoteDiagnostics:
  enabled: true
  targets:
    ${LONG_ID}:
      enabled: true
      environment_group: qa
      connection: {type: ssh, host: 10.40.17.10, port: 22, username: k8s-diagnostics, known_hosts_alias: qa17-control-plane}
      expected_kube_system_uid: 1c43dff0-0000-0000-0000-000000000001
      diagnostic_scopes_allowed: [kubernetes]
      allowed_caller_scopes: [qa-ansible]
      credentialSecretRef: {name: agent-target-credentials}
  callerScopes:
    qa-ansible:
      allowed_target_ids: [${LONG_ID}]
      tokenSecretRef: {name: agent-caller-token}
  knownHostsEntries: ["qa17 ssh-ed25519 AAAA"]
EOF
require_fail "reject over-long target_id" "exceeds K8s 63-char label limit" -f "${LONG_TARGET}"
rm -f "${LONG_TARGET}"

# 8. Rolling-restart annotation must reference the remote-diagnostics CM.
CHECKSUM_OUTPUT=$(helm template test . -f tests/values-remote-direct-ssh.yaml 2>&1)
if ! grep -q "checksum/agent-remote-diagnostics:" <<<"${CHECKSUM_OUTPUT}"; then
  echo "FAIL: backend Deployment missing checksum/agent-remote-diagnostics annotation"
  exit 1
fi
echo "OK: rolling-restart annotation covers remote-diagnostics ConfigMap"

echo
echo "All Phase 6 helm scenarios passed."
