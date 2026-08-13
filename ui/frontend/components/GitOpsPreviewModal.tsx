"use client";

import React from "react";
import type { GitopsPreview } from "@/lib/api";

/**
 * Shows the real diff a PR would contain before anything is pushed. The human
 * approves what they can see, not a promise — the preview endpoint has already
 * located the field and applied the edit in memory, so this diff is exactly
 * what lands in the branch.
 */
export default function GitOpsPreviewModal({
  preview,
  onConfirm,
  onCancel,
}: {
  preview: GitopsPreview;
  onConfirm: (token: string) => void;
  onCancel: () => void;
}) {
  return (
    <div role="dialog" aria-label="Pull request preview" style={{ padding: "1rem", maxWidth: "48rem" }}>
      <h2 style={{ fontSize: "1rem", margin: "0 0 0.5rem" }}>{preview.title}</h2>
      <p style={{ color: "var(--ink-3)", fontSize: "0.8125rem", margin: "0 0 0.5rem" }}>
        Branch <code>{preview.branch}</code>
      </p>
      <pre
        style={{
          background: "var(--surface-2, rgba(0,0,0,0.04))",
          padding: "0.75rem",
          borderRadius: "0.5rem",
          overflowX: "auto",
          fontSize: "0.75rem",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {preview.diff}
      </pre>
      <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem" }}>
        <button type="button" onClick={() => onConfirm(preview.preview_token)}>
          Open pull request →
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
