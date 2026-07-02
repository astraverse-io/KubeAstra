#!/usr/bin/env bash
# verify_remote_diagnostics_target.sh — Phase 0 preflight checks
#
# Runs the automatable steps of the staging parity checklist:
#   docs/ANSIBLE_REMOTE_DIAGNOSTICS_STAGING_CHECKLIST.md
#
# Subcommands:
#   dns      <host>                        — resolve host from this pod
#   tcp      <host> [port]                 — dial TCP (default port 22)
#   hostkey  <host> [port] <expected-fp>   — ssh-keyscan and compare
#                                            SHA-256 fingerprint
#   all      <host> [port] <expected-fp>   — dns + tcp + hostkey
#
# Design:
#   Meant to run from inside a debug pod that mirrors the backend pod's
#   network egress. On the operator laptop this validates DNS and a
#   different-network TCP path — so pod-side is the source of truth.
#
#   To run pod-side, either:
#     A) mount this script into a debug pod:
#          kubectl debug -n <ns> <backend-pod> \
#             --image=alpine:3.20 --profile=general \
#             -it -- sh -c "apk add openssh-client curl bash && bash"
#          # then copy or curl this file in
#     B) or bake it into the backend image at /scripts/ and exec into the
#        running backend pod (see Section 7.5 of the checklist).
#
# Exit codes:
#   0  — every requested check passed
#   1  — a check failed (mismatch, timeout, unresolvable, etc.)
#   2  — usage error
#
# Never prints private key material, tokens, or any secret input. The
# expected fingerprint is a SHA-256 digest of a public key and is safe to
# log.
set -euo pipefail

_die()   { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
_ok()    { printf 'OK:   %s\n' "$*"; }
_info()  { printf '      %s\n' "$*"; }
_usage() {
  cat >&2 <<'USAGE'
usage:
  verify_remote_diagnostics_target.sh dns     <host>
  verify_remote_diagnostics_target.sh tcp     <host> [port]
  verify_remote_diagnostics_target.sh hostkey <host> [port] <expected-sha256-fp>
  verify_remote_diagnostics_target.sh all     <host> [port] <expected-sha256-fp>

  <expected-sha256-fp> format: SHA256:<base64>
  (from: ssh-keygen -l -E sha256 -f /etc/ssh/ssh_host_ed25519_key.pub)
USAGE
  exit 2
}

_need_bin() {
  local bin="$1"; local hint="${2:-}"
  command -v "${bin}" >/dev/null 2>&1 || _die \
    "missing dependency: ${bin}${hint:+ — ${hint}}"
}

cmd_dns() {
  local host="${1:-}"
  [[ -n "${host}" ]] || _usage
  # Portable DNS lookup — getent on Linux (debug pod), python3 or host on
  # macOS (laptop-side fallback). We rely on standard library only so the
  # script works in a stock alpine debug pod after ``apk add openssh-client``.
  local addrs=""
  if command -v getent >/dev/null 2>&1; then
    addrs=$(getent hosts "${host}" 2>/dev/null | awk '{print $1}' | paste -sd, -)
  elif command -v python3 >/dev/null 2>&1; then
    addrs=$(python3 - "${host}" <<'PY' 2>/dev/null || true
import socket, sys
try:
    infos = socket.getaddrinfo(sys.argv[1], None)
    print(",".join(sorted({i[4][0] for i in infos})))
except socket.gaierror:
    sys.exit(1)
PY
)
  elif command -v host >/dev/null 2>&1; then
    addrs=$(host "${host}" 2>/dev/null | awk '/has address|has IPv6 address/ {print $NF}' | paste -sd, -)
  else
    _die "no DNS resolver available (need getent, python3, or host)"
  fi

  if [[ -n "${addrs}" ]]; then
    _ok "DNS resolves ${host} → ${addrs}"
  else
    _die "DNS does not resolve ${host} from this pod"
  fi
}

cmd_tcp() {
  local host="${1:-}"; local port="${2:-22}"
  [[ -n "${host}" ]] || _usage
  # ``bash`` /dev/tcp works in busybox+bash and full bash. We do NOT use
  # ``nc`` because its exit semantics vary across implementations.
  local deadline=5
  if timeout "${deadline}" bash -c "</dev/tcp/${host}/${port}" 2>/dev/null; then
    _ok "TCP dial to ${host}:${port} completed within ${deadline}s"
  else
    _die "TCP dial to ${host}:${port} failed within ${deadline}s — check NetworkPolicy egress and destination firewall"
  fi
}

cmd_hostkey() {
  local host="${1:-}"; local port="${2:-22}"; local expected="${3:-}"
  [[ -n "${host}" && -n "${expected}" ]] || _usage
  # Format check — accepting only SHA256:<base64> prevents accidental
  # MD5-fingerprint pastes (which are the OpenSSH default without -E).
  [[ "${expected}" == SHA256:* ]] || _die \
    "expected fingerprint must start with 'SHA256:' — got ${expected}"

  _need_bin ssh-keyscan  "install openssh-client"
  _need_bin ssh-keygen   "install openssh-client"

  local keyscan_output
  if ! keyscan_output=$(
    ssh-keyscan -T 5 -t ed25519 -p "${port}" "${host}" 2>/dev/null
  ); then
    _die "ssh-keyscan against ${host}:${port} returned no key — target unreachable or refusing SSH"
  fi
  [[ -n "${keyscan_output}" ]] || _die \
    "ssh-keyscan against ${host}:${port} produced empty output"

  # ssh-keygen -l reads a known_hosts-format line from stdin and prints
  # ``<bits> <fingerprint> <comment> (<type>)``. Take field 2.
  local actual
  actual=$(printf '%s\n' "${keyscan_output}" \
    | ssh-keygen -l -E sha256 -f - 2>/dev/null \
    | awk 'NR==1 {print $2}')
  [[ -n "${actual}" ]] || _die \
    "could not extract SHA-256 fingerprint from the presented key"

  if [[ "${actual}" == "${expected}" ]]; then
    _ok "host key fingerprint matches for ${host}:${port}"
    _info "presented: ${actual}"
  else
    printf 'FAIL: host key MISMATCH for %s:%s\n' "${host}" "${port}" >&2
    printf '      expected: %s\n' "${expected}" >&2
    printf '      actual:   %s\n' "${actual}" >&2
    printf '      — do NOT proceed. Either the host was reprovisioned or the pod is talking to the wrong destination.\n' >&2
    exit 1
  fi
}

cmd_all() {
  local host="${1:-}"; local port="${2:-22}"; local expected="${3:-}"
  [[ -n "${host}" && -n "${expected}" ]] || _usage
  cmd_dns "${host}"
  cmd_tcp "${host}" "${port}"
  cmd_hostkey "${host}" "${port}" "${expected}"
  printf '\nAll checks passed for %s:%s.\n' "${host}" "${port}"
}

subcmd="${1:-}"; shift || true
case "${subcmd}" in
  dns)     cmd_dns "$@" ;;
  tcp)     cmd_tcp "$@" ;;
  hostkey) cmd_hostkey "$@" ;;
  all)     cmd_all "$@" ;;
  ""|-h|--help) _usage ;;
  *) printf 'unknown subcommand: %s\n' "${subcmd}" >&2; _usage ;;
esac
