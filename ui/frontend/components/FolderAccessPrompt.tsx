"use client";

import React, { useCallback, useState } from "react";
import { createFolderGrant, type FolderGrant } from "../lib/api";

/**
 * Consent prompt shown when the desktop agent asks to read a local folder it
 * hasn't been granted (the `access_required` SSE event). The agent only
 * *suggests* a path; the human picks the actual root in the native OS dialog
 * (tauri-plugin-dialog), which is authoritative. On allow we create the grant
 * and hand it back so the caller can resume the suspended run.
 *
 * Desktop-only. The native picker is reached through the Tauri global at
 * runtime, so this component never build-imports a Tauri package; if the global
 * isn't present (e.g. running the Next app in a browser) it degrades to a manual
 * absolute-path field.
 */

export type FolderAccessRequest = {
  path: string;
  mode: "read" | "write";
  reason?: string;
  runId?: string;
  stepId?: number;
};

type Props = {
  request: FolderAccessRequest;
  onGrant: (grant: FolderGrant) => void;
  onDeny: () => void;
};

// Tauri v2 (with `withGlobalTauri`) exposes plugins on window.__TAURI__.
// We look it up defensively and return the picked absolute path, or null.
async function openNativeFolderPicker(defaultPath: string): Promise<string | null> {
  try {
    const tauri = (window as unknown as { __TAURI__?: any }).__TAURI__;
    const open = tauri?.dialog?.open;
    if (typeof open !== "function") return null;
    const picked = await open({ directory: true, multiple: false, defaultPath });
    if (typeof picked === "string") return picked;
    if (Array.isArray(picked) && typeof picked[0] === "string") return picked[0];
    return null;
  } catch {
    return null;
  }
}

export function FolderAccessPrompt({ request, onGrant, onDeny }: Props) {
  const [selectedPath, setSelectedPath] = useState(request.path || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickerUnavailable, setPickerUnavailable] = useState(false);

  const choose = useCallback(async () => {
    setError(null);
    const picked = await openNativeFolderPicker(selectedPath || request.path);
    if (picked) {
      setSelectedPath(picked);
    } else {
      // No native picker (or the user cancelled). Reveal the manual field so the
      // flow is never a dead end.
      setPickerUnavailable(true);
    }
  }, [selectedPath, request.path]);

  const allow = useCallback(async () => {
    const root = selectedPath.trim();
    if (!root) {
      setError("Pick a folder (or enter its absolute path) first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const grant = await createFolderGrant(root, request.mode);
      onGrant(grant);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [selectedPath, request.mode, onGrant]);

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="folder-access-title" style={overlay}>
      <div style={card}>
        <h2 id="folder-access-title" style={titleStyle}>
          Agent wants to {request.mode.toUpperCase()} a local folder
        </h2>
        {request.reason ? <p style={reasonStyle}>Reason: {request.reason}</p> : null}

        <p style={label}>Suggested path (you choose the real root):</p>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <input
            aria-label="Folder path"
            value={selectedPath}
            onChange={(e) => setSelectedPath(e.target.value)}
            placeholder="/absolute/path/to/folder"
            spellCheck={false}
            style={input}
            readOnly={!pickerUnavailable && !!selectedPath}
          />
          <button type="button" onClick={choose} disabled={busy} style={secondaryBtn}>
            Choose folder…
          </button>
        </div>
        {pickerUnavailable ? (
          <p style={hint}>Native picker unavailable — type or paste the absolute folder path above.</p>
        ) : null}

        {error ? <p style={errorStyle}>{error}</p> : null}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1rem" }}>
          <button type="button" onClick={onDeny} disabled={busy} style={ghostBtn}>
            Deny
          </button>
          <button type="button" onClick={allow} disabled={busy} style={primaryBtn}>
            {busy ? "Granting…" : `Allow ${request.mode}`}
          </button>
        </div>
      </div>
    </div>
  );
}

const overlay: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
  padding: "1rem",
};
const card: React.CSSProperties = {
  background: "var(--bg-1, #16181d)",
  color: "var(--fg-0, #e8eaed)",
  border: "1px solid var(--border-0, #2a2d34)",
  borderRadius: "0.75rem",
  padding: "1.25rem",
  width: "min(560px, 100%)",
  boxShadow: "0 12px 40px rgba(0,0,0,0.45)",
};
const titleStyle: React.CSSProperties = { margin: "0 0 0.25rem", fontSize: "1.05rem" };
const reasonStyle: React.CSSProperties = { margin: "0 0 0.75rem", color: "var(--fg-1, #9aa0aa)", fontSize: "0.875rem" };
const label: React.CSSProperties = { margin: "0.5rem 0 0.25rem", fontSize: "0.8125rem", color: "var(--fg-1, #9aa0aa)" };
const input: React.CSSProperties = {
  flex: 1,
  padding: "0.5rem 0.625rem",
  borderRadius: "0.5rem",
  border: "1px solid var(--border-0, #2a2d34)",
  background: "var(--bg-0, #0f1115)",
  color: "var(--fg-0, #e8eaed)",
  fontFamily: "var(--font-mono, ui-monospace, monospace)",
  fontSize: "0.8125rem",
};
const hint: React.CSSProperties = { margin: "0.375rem 0 0", fontSize: "0.75rem", color: "var(--fg-2, #6b7280)" };
const errorStyle: React.CSSProperties = { margin: "0.625rem 0 0", color: "var(--danger, #f87171)", fontSize: "0.8125rem" };
const baseBtn: React.CSSProperties = {
  padding: "0.5rem 0.875rem",
  borderRadius: "0.5rem",
  border: "1px solid var(--border-0, #2a2d34)",
  cursor: "pointer",
  fontSize: "0.8125rem",
};
const primaryBtn: React.CSSProperties = { ...baseBtn, background: "var(--accent, #3b82f6)", color: "#fff", borderColor: "transparent" };
const secondaryBtn: React.CSSProperties = { ...baseBtn, background: "var(--bg-2, #1f2229)", color: "var(--fg-0, #e8eaed)" };
const ghostBtn: React.CSSProperties = { ...baseBtn, background: "transparent", color: "var(--fg-1, #9aa0aa)" };

export default FolderAccessPrompt;
