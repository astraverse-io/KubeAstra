"use client";

/**
 * Connect a GitHub repo so the agent can propose fixes as pull requests.
 *
 * Static-export safe: a plain client component on a static route, no dynamic
 * segment, state read from React not the URL. The PAT itself is never entered
 * here — it lives in the server env (K8s Secret) or the desktop keychain. This
 * page only records which repo to aim at.
 */

import React, { useCallback, useEffect, useState } from "react";

import {
  connectGitopsRepo,
  deleteGitopsRepo,
  getGitopsRepos,
  type GitopsRepo,
} from "@/lib/api";

export default function GitopsSettingsPage() {
  const [repos, setRepos] = useState<GitopsRepo[]>([]);
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("main");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    getGitopsRepos()
      .then((body) => {
        setRepos(body.repos);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const connect = useCallback(async () => {
    if (!owner.trim() || !name.trim()) return;
    setBusy(true);
    try {
      await connectGitopsRepo({
        provider: "github",
        owner: owner.trim(),
        name: name.trim(),
        default_branch: defaultBranch.trim() || "main",
      });
      setOwner("");
      setName("");
      setError(null);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [owner, name, defaultBranch, load]);

  const disconnect = useCallback(
    async (id: string) => {
      setBusy(true);
      try {
        await deleteGitopsRepo(id);
        load();
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  return (
    <main style={{ padding: "1.5rem", maxWidth: "48rem", margin: "0 auto" }}>
      <h1 style={{ fontSize: "1.125rem", margin: "0 0 0.5rem" }}>GitOps repositories</h1>
      <p style={{ color: "var(--ink-3)", fontSize: "0.8125rem", marginTop: 0 }}>
        Connect a GitHub repo and the agent can propose a fix as a pull request
        instead of applying it to the cluster. The access token is configured on
        the server (or, in the desktop app, in your OS keychain) — it is never
        entered on this page.
      </p>

      {error && (
        <p style={{ color: "var(--danger, #b3261e)", fontSize: "0.875rem" }}>{error}</p>
      )}

      <section style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", margin: "1rem 0" }}>
        <input
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          placeholder="owner (e.g. astraverse-io)"
          style={inputStyle}
        />
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="repo (e.g. kubeastra-demo)"
          style={inputStyle}
        />
        <input
          value={defaultBranch}
          onChange={(e) => setDefaultBranch(e.target.value)}
          placeholder="default branch"
          style={{ ...inputStyle, maxWidth: "9rem" }}
        />
        <button type="button" onClick={connect} disabled={busy} style={buttonStyle}>
          Connect
        </button>
      </section>

      {repos.length === 0 ? (
        <p style={{ color: "var(--ink-3)", fontSize: "0.875rem" }}>No repositories connected.</p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {repos.map((repo) => (
            <li
              key={repo.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                borderTop: "1px solid var(--rule)",
                padding: "0.5rem 0",
              }}
            >
              <span style={{ fontSize: "0.875rem" }}>
                {repo.owner}/{repo.name}
                <span style={{ color: "var(--ink-3)" }}> · {repo.default_branch}</span>
              </span>
              <button
                type="button"
                onClick={() => disconnect(repo.id)}
                disabled={busy}
                style={buttonStyle}
              >
                Disconnect
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

const inputStyle: React.CSSProperties = {
  fontSize: "0.8125rem",
  padding: "0.375rem 0.5rem",
  borderRadius: "0.5rem",
  border: "1px solid var(--rule)",
  background: "transparent",
  color: "inherit",
  fontFamily: "inherit",
  flex: "1 1 12rem",
};

const buttonStyle: React.CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--ink-3)",
  background: "transparent",
  border: "1px solid var(--rule)",
  padding: "0.375rem 0.625rem",
  borderRadius: "0.5rem",
  cursor: "pointer",
  fontFamily: "inherit",
};
