# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

# SPECPATH is provided by PyInstaller at build time
SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = (SPEC_DIR / ".." / "..").resolve()
BACKEND_DIR = (REPO_ROOT / "ui" / "backend").resolve()
MCP_DIR = (REPO_ROOT / "mcp").resolve()

block_cipher = None

# ── Collecting mcp/ ───────────────────────────────────────────────────────
# The mcp/ tree ships as *data* (it is put on sys.path at runtime rather than
# imported as a package — there is no mcp/__init__.py). It must be collected
# file by file, NOT as a whole directory.
#
# A previous revision used datas=[(MCP_DIR, 'mcp')], which swept the entire
# working directory into the installer. A real build put the developer's
# gitignored `mcp/.env` inside the shipped .app and .dmg — and because
# config/settings.py reads `_PROJECT_ROOT / ".env"`, those values would have
# been *applied* on every user's machine, not merely present. mcp/tests/,
# scripts/, setup.sh and __pycache__ shipped too.
#
# Anything added here must be an allow-list. Never re-introduce a bare
# directory entry.

_SKIP_DIRS = {
    '__pycache__', 'tests', 'scripts', 'venv', '.venv', 'node_modules',
    '.git', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov',
}
_SKIP_SUFFIXES = {'.pyc', '.pyo', '.log', '.db', '.sqlite3', '.sqlite'}


def collect_tree(root, dest_prefix, only_suffixes=None, skip_top=()):
    """Collect files under `root` into `dest_prefix`, excluding dev cruft.

    Dotfiles are always skipped — that is what keeps .env, .gitignore and
    .DS_Store out of the bundle.
    """
    collected = []
    root = Path(root)
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] in skip_top:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if any(part.startswith('.') for part in rel.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        if only_suffixes is not None and path.suffix not in only_suffixes:
            continue
        collected.append((str(path), str(Path(dest_prefix) / rel.parent)))
    return collected


mcp_datas = (
    # Source modules, everywhere except data/ (handled below).
    collect_tree(MCP_DIR, 'mcp', only_suffixes={'.py'}, skip_top=('data',))
    # Playbooks and other runtime assets, whatever their extension.
    + collect_tree(MCP_DIR / 'data', 'mcp/data')
)

a = Analysis(
    [str(BACKEND_DIR / 'desktop_main.py')],
    pathex=[str(BACKEND_DIR), str(MCP_DIR)],
    datas=[
        *mcp_datas,
        (str(REPO_ROOT / 'ui' / 'frontend' / 'out'), 'frontend'),
    ],
    hiddenimports=[
        # Routers
        *[f'routers.{m}' for m in (
            'ai_tools kubectl recovery health chat sessions cluster feedback '
            'models alerts agent_runs admin agent metrics auth desktop'
        ).split()],
        # MCP modules & services
        'ai_tools',
        'alerts',
        'config',
        'data',
        'k8s',
        'mcp_server',
        'services',
        'tool_registry',
        'services.rag.capture',
        'services.rag.redaction',
        'services.rag.router',
        # Providers are chosen at runtime by get_provider(), so none of these
        # are statically reachable. The module names carry the _provider
        # suffix — an earlier revision listed 'services.llm.anthropic' etc.,
        # which do not exist and errored on every build.
        'services.llm',
        'services.llm.base',
        'services.llm.pricing',
        'services.llm.anthropic_provider',
        'services.llm.openai_provider',
        'services.llm.gemini_provider',
        'services.llm.ollama_provider',
        'services.embeddings',
        'services.vector_db',
        'desktop_security',
        'desktop_secrets',
        'desktop_paths',
        # Keyring backends
        'keyring.backends.macOS',
        'keyring.backends.Windows',
        'keyring.backends.SecretService',
        'keyring.backends.kwallet',
        'keyring.backends.chainer',
        # Uvicorn dynamic imports
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # Qdrant client & embedded storage
        'qdrant_client',
        'qdrant_client.local',
        'qdrant_client.local.qdrant_local',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'sentence_transformers', 'transformers', 'scipy', 'matplotlib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='kubeastra-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX rewrites Mach-O headers, which invalidates code signatures and has
    # a long history of producing binaries that fail notarization. A signed
    # release is the entire point of this bundle, so it stays off.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    # UPX rewrites Mach-O headers, which invalidates code signatures and has
    # a long history of producing binaries that fail notarization. A signed
    # release is the entire point of this bundle, so it stays off.
    upx=False,
    upx_exclude=[],
    name='kubeastra-backend',
)
