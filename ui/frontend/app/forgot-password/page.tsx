"use client";

import { useState } from "react";
import Link from "next/link";
import { ApiError, forgotPassword } from "../../lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  const submit = async () => {
    if (!email.trim()) return;
    setBusy(true);
    setError("");
    try {
      await forgotPassword(email.trim());
      setSent(true);
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
        <h1 style={{ margin: "0 0 0.25rem 0", fontSize: "1rem", fontWeight: 700 }}>Reset your password</h1>
        <p style={{ margin: "0 0 1.25rem 0", color: "var(--ink-3)", fontSize: "0.75rem" }}>
          Enter your account email and we&apos;ll send you a link to reset your password.
        </p>

        {sent ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            <p style={{ margin: 0, color: "var(--ink-2)", fontSize: "0.8125rem" }}>
              If an account with that email exists, a reset link has been sent. Check your inbox
              and follow the link to choose a new password.
            </p>
            <Link href="/chat" className="app-btn-primary" style={{ padding: "0.5rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, textAlign: "center", textDecoration: "none" }}>
              Back to sign in
            </Link>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
              Email address
              <input
                className="app-input"
                type="email"
                value={email}
                placeholder="you@example.com"
                autoFocus
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submit();
                }}
              />
            </label>
            {error && <p style={{ color: "var(--danger)", fontSize: "0.75rem", margin: 0 }}>{error}</p>}
            <button
              className="app-btn-primary"
              disabled={busy || !email.trim()}
              onClick={submit}
              style={{ padding: "0.625rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, opacity: busy ? 0.6 : 1 }}
            >
              {busy ? "Sending..." : "Send reset link"}
            </button>
            <Link href="/chat" style={{ fontSize: "0.75rem", color: "var(--ink-3)", textAlign: "center", textDecoration: "none" }}>
              Back to sign in
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
