"""The Dockerfile must copy in every requirements file it transitively needs.

A requirements file may include another with `-r other.txt`, and pip resolves
that relative to the *including file's own directory* — not the working
directory. So a Dockerfile that copies `mcp/requirements.txt` to a flat
`/tmp/mcp-requirements.txt` silently breaks the include.

That is not hypothetical. It is what happened when the desktop dependency
split added `-r requirements-core.txt`: every backend image build failed from
2026-07-29 to 2026-08-06 while CI stayed green, because CI installs from the
repo tree where the include resolves fine. Nothing built the image until
release.yml did, on `main`, after the merge.

CI now builds the image, which catches this directly. This test catches it in
seconds instead of minutes, and says which include broke rather than leaving a
pip error to be read out of a buildx log.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "ui" / "backend" / "Dockerfile"


def _copy_instructions(text: str) -> list[tuple[list[str], str]]:
    """Return (sources, destination) for each COPY, joining line continuations."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    instructions = []
    for line in joined.splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        if len(parts) >= 2:
            instructions.append((parts[:-1], parts[-1]))
    return instructions


def _includes(requirements: Path) -> list[str]:
    """The `-r`/`--requirement` targets declared inside a requirements file."""
    found = []
    for raw in requirements.read_text().splitlines():
        match = re.match(r"\s*(?:-r|--requirement)[=\s]+(\S+)", raw)
        if match:
            found.append(match.group(1))
    return found


def _image_paths() -> dict[str, Path]:
    """Map each in-image path to the repo file that lands there.

    Only requirements files matter here, and only the COPY forms this
    Dockerfile actually uses: a single source renamed to a file, or several
    sources into a directory (trailing slash).
    """
    landed: dict[str, Path] = {}
    for sources, destination in _copy_instructions(DOCKERFILE.read_text()):
        for source in sources:
            if not source.endswith(".txt"):
                continue
            repo_file = REPO_ROOT / source
            if destination.endswith("/"):
                landed[destination + Path(source).name] = repo_file
            else:
                landed[destination] = repo_file
    return landed


def test_the_dockerfile_copies_requirements_at_all():
    """A guard on the guard: if the COPY parsing stops matching the Dockerfile,
    every assertion below passes vacuously."""
    assert _image_paths(), "parsed no requirements COPY out of the Dockerfile"


@pytest.mark.parametrize(
    "image_path,repo_file", sorted((k, v) for k, v in _image_paths().items())
)
def test_every_include_resolves_inside_the_image(image_path: str, repo_file: Path):
    assert repo_file.exists(), f"{repo_file} is COPYed but does not exist"

    for include in _includes(repo_file):
        # pip resolves the include against the including file's directory,
        # which after COPY is its directory *in the image*, not in the repo.
        resolved = str((Path(image_path).parent / include).as_posix())
        assert resolved in _image_paths(), (
            f"{repo_file.relative_to(REPO_ROOT)} includes '{include}', which pip "
            f"will look for at {resolved} in the image — but nothing is COPYed "
            f"there. The image build will fail even though installing from the "
            f"repo tree works."
        )


# ── what must never reach a published image ───────────────────────────────

# Build contexts, as release.yml and ci.yml pass them. The backend builds from
# the repository root, so its context contains every developer artefact in the
# checkout.
BUILD_CONTEXTS = {
    "backend": REPO_ROOT,
    "frontend": REPO_ROOT / "ui" / "frontend",
}

# Real files that exist in a working checkout and are gitignored — which is
# exactly why CI never noticed them. `COPY ui/backend/ /app/` put all three
# into an image built locally: ten live API keys and 18MB of chat history.
SECRETS_THAT_ARE_REALLY_THERE = ("**/.env", "**/*.db")


@pytest.mark.parametrize("name,context", sorted(BUILD_CONTEXTS.items()))
def test_every_build_context_has_a_dockerignore(name: str, context: Path):
    """Without one, `docker build` sends the whole directory and the Dockerfile's
    directory-level COPYs bake in whatever the builder happens to have."""
    assert (context / ".dockerignore").exists(), (
        f"the {name} image builds from {context}, which has no .dockerignore — "
        f"a local build will copy in developer state"
    )


@pytest.mark.parametrize("pattern", SECRETS_THAT_ARE_REALLY_THERE)
def test_the_root_context_excludes_secrets_and_local_databases(pattern: str):
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert pattern in lines, (
        f"'{pattern}' is not excluded from the backend build context. The "
        f"deployment guide tells readers to build this image themselves, so a "
        f"missing exclusion leaks the builder's own credentials into a layer "
        f"they then push to a registry."
    )


def test_the_example_env_is_still_allowed_through():
    """It is committed, carries no secrets, and the docs point at it — a blanket
    `.env*` exclusion that swallows it would be a silent docs break."""
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert any(line.strip() == "!**/.env.example" for line in lines)


def test_the_known_include_is_the_one_being_guarded():
    """Pins the case this was written for, so that dropping the `-r` from
    mcp/requirements.txt turns this file into a no-op loudly rather than
    quietly."""
    assert "requirements-core.txt" in _includes(REPO_ROOT / "mcp" / "requirements.txt")
