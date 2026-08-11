"""The release workflow must pick exactly one signing/notarization path.

A missing GitHub secret is an EMPTY STRING, not an unset variable. Tauri reads
its credentials with the Rust equivalent of `env::var`, which returns `Ok("")`
for a variable that was set to nothing — so naming a secret in `env:` is enough
to make Tauri believe it has that credential and take the wrong branch.

release-desktop.yml already carries a long comment about this: passing
APPLE_CERTIFICATE when no such secret exists produced

    failed codesign application: failed to run command security import:
    failed to import keychain certificate

There is no way to conditionally omit a single `env` key, so the only fix is
one step per credential combination, each guarded by an `if`. That makes the
step conditions load-bearing, and nothing else checks them — a tagged release
is the first thing that would, and by then the tag exists.

Notarization has the same shape as signing. Apple takes either an app-specific
password (APPLE_ID + APPLE_PASSWORD) or an App Store Connect API key
(APPLE_API_ISSUER + APPLE_API_KEY + APPLE_API_KEY_PATH). Tauri prefers the API
key when it sees one, so a blank APPLE_API_KEY alongside a working password
silently breaks a release that would otherwise have worked.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-desktop.yml"

# The two credential families. Naming one variable from either family commits
# Tauri to that family, so a step must draw from at most one of them.
PASSWORD_VARS = {"APPLE_ID", "APPLE_PASSWORD"}
API_KEY_VARS = {"APPLE_API_ISSUER", "APPLE_API_KEY", "APPLE_API_KEY_PATH"}


def _build_steps() -> list[dict]:
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    return [s for s in job["steps"] if str(s.get("name", "")).startswith("Build Tauri App")]


def _job_env() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]["env"]


def test_the_workflow_still_parses():
    """A guard on the guard — every assertion below reads this file."""
    assert WORKFLOW.exists()
    assert _build_steps(), "parsed no build steps; the naming convention changed"


def test_there_is_a_step_for_every_credential_combination():
    """unsigned, signed+password, signed+api-key. Dropping one does not fail a
    build — it silently produces an unsigned release, or none at all."""
    assert len(_build_steps()) == 3


@pytest.mark.parametrize("step", _build_steps(), ids=lambda s: s["name"])
def test_every_build_step_is_guarded(step: dict):
    """An unguarded step runs alongside the one that was supposed to replace
    it, and the release ends up with two builds racing for the same tag."""
    assert step.get("if"), f"{step['name']} has no `if`"


@pytest.mark.parametrize("step", _build_steps(), ids=lambda s: s["name"])
def test_no_step_mixes_the_two_notarization_credential_families(step: dict):
    """The empty-string trap. A step that names both hands Tauri the API-key
    branch whether or not the API key exists."""
    env = set(step.get("env", {}))
    password = env & PASSWORD_VARS
    api_key = env & API_KEY_VARS

    assert not (password and api_key), (
        f"{step['name']} names both {sorted(password)} and {sorted(api_key)}. "
        f"Tauri prefers the API key, so whichever is unset becomes an empty "
        f"string and notarization fails with no useful message. Split this "
        f"into two steps guarded on HAS_APPLE_API_KEY."
    )


@pytest.mark.parametrize(
    "flag", ["HAS_APPLE_CERT", "HAS_UPDATER_KEY", "HAS_APPLE_API_KEY"]
)
def test_secret_presence_is_computed_at_job_level(flag: str):
    """`secrets` is not available in a step-level `if`. Computing these in the
    job `env` is the only place the check works — moved into a step, the
    expression evaluates to an empty string and the step silently never runs.
    """
    assert flag in _job_env()
    assert "secrets." in str(_job_env()[flag])


def test_the_signed_steps_disagree_on_exactly_the_api_key_flag():
    """The two signed paths must be mutually exclusive. If both conditions can
    be true at once, a tagged build runs two full Tauri builds and uploads two
    sets of artifacts to the same draft release."""
    signed = [s for s in _build_steps() if "HAS_APPLE_CERT == 'true'" in s["if"]]

    assert len(signed) == 2
    conditions = {s["if"] for s in signed}
    assert any("HAS_APPLE_API_KEY == 'true'" in c for c in conditions)
    assert any("HAS_APPLE_API_KEY != 'true'" in c for c in conditions)


def test_no_two_build_steps_share_a_condition():
    """Distinct `if` strings are not proof of exclusivity, but two identical
    ones are proof of the opposite."""
    conditions = [s["if"] for s in _build_steps()]

    for a, b in itertools.combinations(conditions, 2):
        assert a != b, f"two build steps share the condition {a!r}"


def test_the_api_key_file_is_written_before_the_build_that_reads_it():
    """APPLE_API_KEY_PATH points at a file. Ordering these wrong gives a
    notarization failure that reads like a credentials problem."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    names = [str(s.get("name", "")) for s in job["steps"]]

    materialize = names.index("Materialize App Store Connect API key")
    build = names.index("Build Tauri App (signed, notarized with an API key)")

    assert materialize < build


# ── the sidecar has to be signed too ──────────────────────────────────────
#
# codesign does not recurse into bundle resources, and the PyInstaller sidecar
# arrives as a resource. Tauri signing the .app therefore leaves ~130 Mach-O
# files carrying PyInstaller's ad-hoc signatures, and Apple refuses the lot:
# desktop-v0.2.0 came back with 192 errors across 96 binaries, every one of
# them under Contents/Resources/binaries/kubeastra-backend/.
#
# Nothing about that is visible before notarization. The build succeeds, the
# DMG opens, and only Apple says otherwise — so these are pinned here.

TAURI_CONF = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
SIGN_SIDECAR = REPO_ROOT / "desktop" / "scripts" / "sign-sidecar.sh"


def _tauri_conf() -> dict:
    import json

    return json.loads(TAURI_CONF.read_text())


def _step(name: str) -> dict:
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    for s in job["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r}")


def test_the_workflow_signs_the_sidecar():
    """Without this the bundler copies unsigned binaries into the .app and the
    failure surfaces only once Apple has looked at it, 90 seconds later."""
    assert "sign-sidecar.sh" in _step("Sign the sidecar binaries")["run"]


def test_the_signing_step_is_not_a_tauri_bundle_hook():
    """beforeBundleCommand is the obvious home for this and it does not work.

    Tauri imports the signing certificate *after* running that hook, so the
    signer found no identity, skipped, and the build failed at notarization as
    though it had never run. The log ordering was unambiguous: "no Developer ID
    identity available" immediately before "1 identity imported".

    Moving it back would look like a tidy-up and cost another full release
    cycle to rediscover.
    """
    hook = _tauri_conf().get("build", {}).get("beforeBundleCommand", "")

    assert "sign-sidecar" not in hook, (
        "the sidecar signer is wired as a Tauri bundle hook again. It cannot "
        "work there — no certificate is imported yet at that point."
    )


def test_the_signing_step_runs_before_the_build_that_bundles_it():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    names = [str(s.get("name", "")) for s in job["steps"]]

    sign = names.index("Sign the sidecar binaries")
    builds = [i for i, n in enumerate(names) if n.startswith("Build Tauri App")]

    assert sign < min(builds), "the sidecar is signed after it has been bundled"


def test_the_signing_step_only_runs_when_a_certificate_exists():
    """Unguarded, it would fail every unsigned build at `security import` —
    and, since the Windows lane was added, every Windows build too, because
    HAS_APPLE_CERT is a secret-presence flag and is equally true there."""
    condition = _step("Sign the sidecar binaries")["if"]

    assert "env.HAS_APPLE_CERT == 'true'" in condition
    assert "startsWith(matrix.platform, 'macos')" in condition


def test_the_signing_step_demands_an_identity_rather_than_skipping():
    """The signer's default is to skip when it finds no identity, which is
    right for a local build and catastrophic here — it is how a release got
    all the way to Apple with 130 unsigned binaries inside it."""
    assert "--require-identity" in _step("Sign the sidecar binaries")["run"]


def test_the_signer_exists_and_is_executable():
    assert SIGN_SIDECAR.exists()
    assert SIGN_SIDECAR.stat().st_mode & 0o111, f"{SIGN_SIDECAR.name} is not executable"


@pytest.mark.parametrize(
    "flag,why",
    [
        ("--force", "PyInstaller leaves ad-hoc signatures; codesign will not replace them without it"),
        ("--timestamp", "'The signature does not include a secure timestamp.' — 192 of them"),
        ("--options runtime", "'The executable does not have the hardened runtime enabled.'"),
    ],
)
def test_the_signer_passes_the_flags_apple_requires(flag: str, why: str):
    assert flag in SIGN_SIDECAR.read_text(), f"missing {flag}: {why}"


def test_frameworks_are_signed_as_bundles_not_as_loose_files():
    """codesign refuses a binary in a framework's interior:

        Python.framework/Python: bundle format is ambiguous
                                 (could be app or framework)

    The sidecar carries Python.framework, so a flat "sign every Mach-O" loop
    dies on it. Framework interiors are skipped and the framework is signed at
    its version directory instead.
    """
    source = SIGN_SIDECAR.read_text()

    assert "*.framework/*) continue" in source, (
        "framework interiors are no longer skipped; codesign will fail with "
        "'bundle format is ambiguous'"
    )
    assert '-name "*.framework"' in source, "frameworks are never signed at all"


def test_a_flattened_framework_is_relinked_before_signing():
    """The sidecar's Python.framework arrives with no symlinks at all — three
    byte-identical copies of the binary at Python.framework/Python,
    Versions/Current/Python and Versions/3.x/Python.

    Signing the version directory covers one of the three. Apple rejected the
    other two by name and nothing else. Relinking is the fix rather than
    signing each copy, because codesign will not sign a binary inside a
    .framework as a loose file, and a framework with duplicated binaries is
    malformed to begin with. It also drops the framework from 16MB to 5.2MB.
    """
    source = SIGN_SIDECAR.read_text()

    assert 'ln -s "$version" "$fw/Versions/Current"' in source
    assert 'ln -s "Versions/Current/$name" "$entry"' in source


def test_the_duplicate_top_level_binary_is_deleted_not_symlinked():
    """Tauri's bundle.resources glob copies file-by-file, and std::fs::copy
    follows a symlink — so a top-level link becomes a plain copy again inside
    the .app, carrying the framework binary's embedded signature at a path
    where nothing seals it. Apple calls that "the signature of the binary is
    invalid", and it was the single error that outlived every other fix.

    Directory symlinks survive, because the glob does not recurse into them:
    Apple stopped reporting Versions/Current/Python the moment that became
    one, while still reporting the top-level file. That asymmetry is the whole
    reason this deletes rather than links.

    Nothing needs the top-level binary — dependents resolve @rpath/Python, and
    the sidecar was verified to start with the file removed.
    """
    source = SIGN_SIDECAR.read_text()

    assert 'rm -f "$entry"' in source, (
        "the duplicate top-level framework binary is being symlinked again "
        "instead of deleted; Tauri will flatten it back into an unsealed copy"
    )


def test_relinking_refuses_to_delete_a_file_that_differs():
    """The relink deletes real files. If a top-level entry is not identical to
    the versioned one, the framework is not the shape this script assumes, and
    deleting from it would destroy something rather than deduplicate it."""
    source = SIGN_SIDECAR.read_text()

    assert "shasum -a 256" in source, (
        "the relink no longer verifies the copies are identical before "
        "replacing them with symlinks"
    )


def test_the_framework_version_is_discovered_not_hardcoded():
    """CI builds on Python 3.11 and a developer checkout may be on anything
    else. A hardcoded Versions/3.11 silently signs nothing on the other one."""
    source = SIGN_SIDECAR.read_text()

    assert "Versions/3.11" not in source
    assert "$fw/Versions" in source


def test_the_entitlements_the_signer_references_exist():
    """A wrong path here fails at bundle time on a release build only."""
    entitlements = REPO_ROOT / "desktop" / "src-tauri" / "entitlements.plist"

    assert entitlements.exists()
    assert "entitlements.plist" in SIGN_SIDECAR.read_text()


def test_a_missing_identity_is_not_an_error():
    """The workflow deliberately produces unsigned builds when no certificate
    secret exists. A signer that exits non-zero without an identity would turn
    that supported path into a failed release.

    This also pins a real bug: `set -e` plus `pipefail` made the identity
    lookup's own grep-found-nothing kill the script silently, exit 1, no
    output.
    """
    import subprocess

    result = subprocess.run(
        ["bash", str(SIGN_SIDECAR)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin", "APPLE_SIGNING_IDENTITY": ""},
    )

    if "no Developer ID identity" in result.stdout:
        assert result.returncode == 0, (
            f"signer exited {result.returncode} with no identity available; "
            f"that breaks the unsigned build path.\nstderr: {result.stderr}"
        )


# ── the DMG wrapper needs notarizing too ──────────────────────────────────


def test_the_dmg_is_notarized_and_stapled():
    """tauri-action notarizes and staples the .app, then wraps it in a DMG and
    stops. Gatekeeper judges the DMG when somebody double-clicks the download:

        KubeAstra_0.2.0_aarch64.dmg: rejected
        source=Unnotarized Developer ID

    The app inside launches cleanly once mounted, so every check that looks at
    the .app passes — while the user still gets "macOS cannot verify this app
    is free from malware".
    """
    run = _step("Notarize and staple the DMG")["run"]

    assert "notarytool submit" in run
    assert "stapler staple" in run


def test_the_dmg_check_asks_the_question_the_user_asks():
    """`spctl -t exec` evaluates an executable and passes on an unstapled disk
    image. `-t open` is what a double-click actually triggers, and is the only
    form that would have caught this."""
    run = _step("Notarize and staple the DMG")["run"]

    assert "-t open" in run
    assert "stapler validate" in run


def test_the_stapled_dmg_replaces_the_one_already_uploaded():
    """tauri-action uploads the DMG before this step runs, so without
    --clobber the release keeps the unstapled copy and every check here passes
    against an artifact nobody downloads."""
    run = _step("Notarize and staple the DMG")["run"]

    assert "gh release upload" in run
    assert "--clobber" in run


# ── the Windows lane ──────────────────────────────────────────────────────
#
# HAS_APPLE_CERT is a secret-presence flag, not a platform flag: it is equally
# 'true' on the Windows runner. Every Apple step therefore needs the platform
# in its condition as well, or the Windows lane runs `security`, `codesign`
# and `xcrun` and dies.


def _matrix() -> list[dict]:
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    return job["strategy"]["matrix"]["include"]


def _fires(condition: str, *, mac: bool, cert: bool, api_key: bool) -> bool:
    """Evaluate a step `if` for one scenario. Crude, but these conditions are
    built from exactly three predicates and nothing else."""
    c = condition.replace("startsWith(matrix.platform, 'macos')", "True" if mac else "False")
    c = c.replace("env.HAS_APPLE_CERT == 'true'", "True" if cert else "False")
    c = c.replace("env.HAS_APPLE_CERT != 'true'", "False" if cert else "True")
    c = c.replace("env.HAS_APPLE_API_KEY == 'true'", "True" if api_key else "False")
    c = c.replace("env.HAS_APPLE_API_KEY != 'true'", "False" if api_key else "True")
    c = c.replace("&&", " and ").replace("||", " or ")
    c = c.replace("!True", "not True").replace("!False", "not False")
    return bool(eval(c))  # noqa: S307 — fixed vocabulary, from a file in this repo


def test_there_is_a_windows_lane():
    platforms = {e["platform"] for e in _matrix()}

    assert any(p.startswith("windows") for p in platforms), "the Windows lane is gone"
    assert any(p.startswith("macos") for p in platforms)


@pytest.mark.parametrize("mac", [True, False], ids=["macos", "windows"])
@pytest.mark.parametrize("cert", [True, False], ids=["cert", "nocert"])
@pytest.mark.parametrize("api_key", [True, False], ids=["apikey", "noapikey"])
def test_exactly_one_build_step_fires(mac: bool, cert: bool, api_key: bool):
    """Two firing means two Tauri builds racing for the same artifacts; zero
    means a release with nothing in it. Neither announces itself."""
    builds = [s for s in _build_steps()]
    hits = [s["name"] for s in builds if _fires(s["if"], mac=mac, cert=cert, api_key=api_key)]

    assert len(hits) == 1, (
        f"{'macOS' if mac else 'Windows'} cert={cert} api_key={api_key} fires "
        f"{len(hits)} build steps: {hits}"
    )


@pytest.mark.parametrize(
    "step_name",
    [
        "Sign the sidecar binaries",
        "Materialize App Store Connect API key",
        "Build Tauri App (signed, notarized with an API key)",
        "Build Tauri App (signed, notarized with an app-specific password)",
        "Notarize and staple the DMG",
    ],
)
def test_every_apple_step_is_gated_on_macos(step_name: str):
    """Without the platform check these run on windows-latest, where
    `security` and `codesign` do not exist."""
    assert "startsWith(matrix.platform, 'macos')" in _step(step_name)["if"], (
        f"{step_name} would run on the Windows runner"
    )


def test_the_job_runs_bash_on_both_platforms():
    """windows-latest defaults to PowerShell, and every `run:` here is bash —
    `set -euo pipefail`, `$RUNNER_TEMP`, `test -f`."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]

    assert job.get("defaults", {}).get("run", {}).get("shell") == "bash"


def test_no_step_hardcodes_slash_tmp():
    """/tmp is not a usable path on the Windows runner; $RUNNER_TEMP is
    defined everywhere."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    offenders = [
        s.get("name", "?") for s in job["steps"] if "/tmp/" in str(s.get("run", ""))
    ]

    assert offenders == [], f"these hardcode /tmp: {offenders}"


def test_the_p8_secret_is_never_named_by_a_build_step():
    """The key material is decoded to a file by one step. A build step that
    also named it would put the raw key into the environment of a third-party
    action for no reason."""
    for step in _build_steps():
        assert "APPLE_API_KEY_P8" not in set(step.get("env", {})), (
            f"{step['name']} exposes the raw .p8 to tauri-action"
        )
