"use client";

import { useState } from "react";
import {
  ApiError,
  changePassword,
  updateEmail,
  type AuthStatus,
  type AuthUser,
} from "../lib/api";

/** Friendly message for any thrown error, with a clear note for rate limiting. */
function errorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 429) {
    return "Too many attempts. Please try again later.";
  }
  return err instanceof Error ? err.message : String(err);
}

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  fontSize: "0.75rem",
  color: "var(--ink-3)",
};

interface AccountSettingsProps {
  user: AuthUser;
  onClose: () => void;
  /** Called after the email is updated so the parent can refresh auth state. */
  onAuthChanged: (status: AuthStatus) => void;
}

export default function AccountSettings({ user, onClose, onAuthChanged }: AccountSettingsProps) {
  // ── email ──
  const [email, setEmail] = useState(user.email ?? "");
  const [emailPassword, setEmailPassword] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailError, setEmailError] = useState("");
  const [emailOk, setEmailOk] = useState("");

  // ── password ──
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState("");
  const [pwOk, setPwOk] = useState("");

  const submitEmail = async () => {
    if (!emailPassword) return;
    setEmailBusy(true);
    setEmailError("");
    setEmailOk("");
    try {
      const next = await updateEmail(email.trim() || null, emailPassword);
      onAuthChanged(next);
      setEmailPassword("");
      setEmailOk(email.trim() ? "Email updated." : "Email removed.");
    } catch (err) {
      setEmailError(errorMessage(err));
    } finally {
      setEmailBusy(false);
    }
  };

  const submitPassword = async () => {
    if (!currentPassword || !newPassword) return;
    if (newPassword !== confirmPassword) {
      setPwError("New passwords do not match.");
      return;
    }
    setPwBusy(true);
    setPwError("");
    setPwOk("");
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPwOk("Password changed. Other sessions have been signed out.");
    } catch (err) {
      setPwError(errorMessage(err));
    } finally {
      setPwBusy(false);
    }
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.55)",
        padding: "1rem",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth: "26rem",
          maxHeight: "90vh",
          overflowY: "auto",
          borderRadius: "1rem",
          padding: "1.5rem",
          background: "var(--paper-2)",
          border: "1px solid var(--rule)",
          boxShadow: "0 20px 60px rgba(0,0,0,0.35)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1.25rem" }}>
          <h2 style={{ margin: 0, fontSize: "1rem", fontWeight: 700 }}>Account settings</h2>
          <button
            className="app-btn-ghost"
            onClick={onClose}
            aria-label="Close"
            style={{ padding: "0.25rem 0.5rem", borderRadius: "0.5rem", fontSize: "0.875rem" }}
          >
            ✕
          </button>
        </div>

        {/* ── Email ── */}
        <section style={{ display: "flex", flexDirection: "column", gap: "0.75rem", marginBottom: "1.5rem" }}>
          <div>
            <h3 style={{ margin: 0, fontSize: "0.8125rem", fontWeight: 600 }}>Email</h3>
            <p style={{ margin: "0.25rem 0 0 0", color: "var(--ink-3)", fontSize: "0.6875rem" }}>
              Used to send password-reset links. Changing it requires your current password.
            </p>
          </div>
          <label style={labelStyle}>
            Email address
            <input
              className="app-input"
              type="email"
              value={email}
              placeholder="you@example.com"
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label style={labelStyle}>
            Current password
            <input
              className="app-input"
              type="password"
              value={emailPassword}
              onChange={(e) => setEmailPassword(e.target.value)}
            />
          </label>
          {emailError && <p style={{ color: "var(--danger)", fontSize: "0.75rem", margin: 0 }}>{emailError}</p>}
          {emailOk && <p style={{ color: "var(--brand)", fontSize: "0.75rem", margin: 0 }}>{emailOk}</p>}
          <button
            className="app-btn-primary"
            disabled={emailBusy || !emailPassword}
            onClick={submitEmail}
            style={{ padding: "0.5rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, opacity: emailBusy ? 0.6 : 1 }}
          >
            {emailBusy ? "Saving..." : "Save email"}
          </button>
        </section>

        <div style={{ borderTop: "1px solid var(--rule)", margin: "0 0 1.5rem 0" }} />

        {/* ── Password ── */}
        <section style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <h3 style={{ margin: 0, fontSize: "0.8125rem", fontWeight: 600 }}>Change password</h3>
          <label style={labelStyle}>
            Current password
            <input
              className="app-input"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
          </label>
          <label style={labelStyle}>
            New password
            <input
              className="app-input"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </label>
          <label style={labelStyle}>
            Confirm new password
            <input
              className="app-input"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitPassword();
              }}
            />
          </label>
          {pwError && <p style={{ color: "var(--danger)", fontSize: "0.75rem", margin: 0 }}>{pwError}</p>}
          {pwOk && <p style={{ color: "var(--brand)", fontSize: "0.75rem", margin: 0 }}>{pwOk}</p>}
          <button
            className="app-btn-primary"
            disabled={pwBusy || !currentPassword || !newPassword}
            onClick={submitPassword}
            style={{ padding: "0.5rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, opacity: pwBusy ? 0.6 : 1 }}
          >
            {pwBusy ? "Saving..." : "Change password"}
          </button>
        </section>
      </div>
    </div>
  );
}
