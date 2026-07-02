"use client";

import { useState } from "react";
import Link from "next/link";
import { ApiError, resetPassword } from "../../lib/api";

export default function ResetPasswordForm({ token }: { token: string }) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const submit = async () => {
    if (!newPassword) return;
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await resetPassword(token, newPassword);
      setDone(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError("Too many attempts. Please try again later.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--paper)", color: "var(--ink)", padding: "1rem" }}>
      <div style={{ width: "100%", maxWidth: "26rem", borderRadius: "1rem", padding: "1.5rem", background: "var(--paper-2)", border: "1px solid var(--rule)", boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}>
        <h1 style={{ margin: "0 0 1.25rem 0", fontSize: "1rem", fontWeight: 700 }}>Choose a new password</h1>

        {!token ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            <p style={{ margin: 0, color: "var(--danger)", fontSize: "0.8125rem" }}>
              This reset link is missing its token. Request a new link to continue.
            </p>
            <Link href="/forgot-password" className="sym-btn-primary" style={{ padding: "0.5rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, textAlign: "center", textDecoration: "none" }}>
              Request a new link
            </Link>
          </div>
        ) : done ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            <p style={{ margin: 0, color: "var(--brand)", fontSize: "0.8125rem" }}>
              Your password has been reset. All sessions have been signed out.
            </p>
            <Link href="/chat" className="sym-btn-primary" style={{ padding: "0.5rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, textAlign: "center", textDecoration: "none" }}>
              Sign in
            </Link>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
              New password
              <input
                className="sym-input"
                type="password"
                value={newPassword}
                autoFocus
                onChange={(e) => setNewPassword(e.target.value)}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
              Confirm new password
              <input
                className="sym-input"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submit();
                }}
              />
            </label>
            {error && <p style={{ color: "var(--danger)", fontSize: "0.75rem", margin: 0 }}>{error}</p>}
            <button
              className="sym-btn-primary"
              disabled={busy || !newPassword || !confirmPassword}
              onClick={submit}
              style={{ padding: "0.625rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, opacity: busy ? 0.6 : 1 }}
            >
              {busy ? "Resetting..." : "Reset password"}
            </button>
            <Link href="/forgot-password" style={{ fontSize: "0.75rem", color: "var(--ink-3)", textAlign: "center", textDecoration: "none" }}>
              Request a new link
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
