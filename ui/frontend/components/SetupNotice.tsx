"use client";

/**
 * Tells you the assistant has no language model *before* you ask it anything.
 *
 * Without this, an unconfigured install looks fully working until you type a
 * question, wait for it, and get back a root-cause card whose root cause is
 * "LLM not configured" — a setup problem dressed as a diagnosis of your
 * cluster. The only earlier signal was a small grey "AI" dot in the header,
 * which was easy to miss and no longer exists.
 *
 * Server mode only. Desktop mode — including `kubeastra open`, which sets
 * KUBEASTRA_MODE=desktop — has the first-run wizard, so this would never
 * render there.
 *
 * Deliberately not a modal. Everything else works without a model: cluster
 * connection, the kubectl tools, the resource graph. Blocking the screen would
 * overstate the problem. It sits on the empty state, where the reader is
 * already looking, and disappears as soon as a provider answers.
 */

import React from "react";

export type SetupNoticeProps = {
  /** Which provider the backend is configured for, from /api/health. */
  provider?: string | null;
};

export default function SetupNotice({ provider }: SetupNoticeProps) {
  const isOllama = (provider || "").toLowerCase() === "ollama";

  // Each branch names the thing the reader actually has in front of them. One
  // generic "configure a provider" would be true everywhere and useful nowhere.
  const steps: React.ReactNode[] = isOllama
    ? [
        <>Start the server: <code>ollama serve</code></>,
        <>Pull a model: <code>ollama pull llama3.1</code></>,
        <>Point <code>OLLAMA_BASE_URL</code> and <code>OLLAMA_MODEL</code> at it</>,
      ]
    : [
        <>
          Get a free key at{" "}
          <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
            aistudio.google.com
          </a>
        </>,
        <>
          Set <code>GEMINI_API_KEY</code> — a Secret in the Helm chart, or{" "}
          <code>ui/backend/.env</code> under docker-compose
        </>,
        <>
          Restart the backend. To keep everything on your own infrastructure
          instead, set <code>LLM_PROVIDER=ollama</code>
        </>,
      ];

  return (
    <div className="ka-setup-notice" role="status">
      <div className="ka-setup-head">
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 8v5M12 16.5v.01" />
        </svg>
        No language model connected
      </div>
      <p className="ka-setup-body">
        Cluster tools still work — you can connect a cluster and browse
        resources. Questions in plain English need a model.
      </p>
      <ol className="ka-setup-steps">
        {steps.map((step, i) => (
          <li key={i}>{step}</li>
        ))}
      </ol>
    </div>
  );
}
