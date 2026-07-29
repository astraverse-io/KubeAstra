# Tauri sidecar spike

Throwaway. Exists to answer four questions with a running binary rather than
a guess, before the real Phase 2 shell is written:

1. Can Tauri spawn the Python backend as a **sidecar** and read its stdout?
2. Does the **`PORT=<n>` handshake** in `desktop_main.py` parse reliably from
   the Rust side?
3. Does **single-instance** work? (Required, not cosmetic: qdrant-client's
   local mode takes an exclusive lock, so a second backend breaks memory.)
4. Can the webview navigate to **`/auth?token=…`** and end up with a working
   session cookie?

Phase 0 is why this exists: the plan confidently named `main.py` as the
PyInstaller entry point, and it turned out to have no `__main__` block at all.
Unverified assumptions in a spec cost more than a day of spiking.

## Run

```bash
export PATH="$HOME/.cargo/bin:$PATH"
cd desktop/spike/src-tauri
cargo run
```

The first build compiles ~400 crates and takes several minutes.

## Findings

Recorded in `internal_docs/features/DESKTOP_PHASE1_2_SPEC.md` § 2.4 once the
spike has run. Delete this directory when the real shell replaces it.
