"use client";

import { useEffect, useState } from "react";
import {
  clusterAutodetect,
  clusterConnectContext,
  clusterDisconnect,
  clusterUploadKubeconfig,
  type ClusterStatus,
  type KubeContext,
} from "../lib/api";

interface ClusterConnectProps {
  sessionId: string;
  status: ClusterStatus | null;
  onStatusChange: (status: ClusterStatus | null) => void;
  isOpen?: boolean;
  onToggle?: (open: boolean) => void;
  /**
   * Render the control that opens this popover. Given one, the component
   * stops drawing its own "Connect Cluster" / context pill / "Switch" /
   * "Disconnect" row — four controls describing one piece of state — and
   * "Disconnect" moves inside the panel, next to the contexts it applies to.
   * The header passes the target block here.
   */
  trigger?: (args: { open: boolean; toggle: () => void }) => React.ReactNode;
  /** Hand off to the SSH flow — the other way to reach a cluster. */
  onUseSsh?: () => void;
}

export default function ClusterConnect({
  sessionId,
  status,
  onStatusChange,
  isOpen,
  onToggle,
  trigger,
  onUseSsh,
}: ClusterConnectProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = isOpen !== undefined ? isOpen : internalOpen;
  const setOpen = onToggle !== undefined ? onToggle : setInternalOpen;

  const [contexts, setContexts] = useState<KubeContext[]>([]);
  const [currentContext, setCurrentContext] = useState<string | null>(null);
  const [kubeconfigPath, setKubeconfigPath] = useState<string | null>(null);
  const [mode, setMode] = useState<"autodetect" | "kubeconfig-upload">("autodetect");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setBusy(true);
    setError("");
    clusterAutodetect()
      .then((result) => {
        setContexts(result.contexts ?? []);
        setCurrentContext(result.current_context ?? result.contexts?.[0]?.name ?? null);
        setKubeconfigPath(result.kubeconfig_path ?? null);
        if (result.error) setError(result.error);
      })
      .catch((err) => setError(String(err)))
      .finally(() => setBusy(false));
  }, [open]);

  const handleUpload = async () => {
    if (!content.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await clusterUploadKubeconfig(sessionId, content);
      if (result.error) {
        setError(result.error);
        return;
      }
      setMode("kubeconfig-upload");
      setContexts(result.contexts ?? []);
      setCurrentContext(result.current_context ?? result.contexts?.[0]?.name ?? null);
      setKubeconfigPath(result.kubeconfig_path ?? null);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleConnect = async () => {
    if (!currentContext) return;
    setBusy(true);
    setError("");
    try {
      const result = await clusterConnectContext({
        session_id: sessionId,
        context_name: currentContext,
        mode,
        kubeconfig_path: kubeconfigPath,
      });
      if (result.error || !result.connected) {
        setError(result.error || "Connection failed");
        return;
      }
      onStatusChange(result);
      setOpen(false);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setBusy(true);
    setError("");
    try {
      await clusterDisconnect(sessionId);
      onStatusChange(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ position: "relative", display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.75rem" }}>
      {trigger ? (
        trigger({ open, toggle: () => setOpen(!open) })
      ) : status?.connected ? (
        <>
          <span
            style={{
              display: "flex", alignItems: "center", gap: "0.375rem", padding: "0.25rem 0.625rem", borderRadius: "0.5rem",
              border: "1px solid var(--brand-bd)", background: "var(--brand-bg)", color: "var(--brand)"
            }}
          >
            <span style={{ width: "0.375rem", height: "0.375rem", borderRadius: "50%", background: "var(--brand)" }} />
            {status.context_name || status.cluster_name || "cluster"}
          </span>
          <button
            onClick={() => setOpen(!open)}
            disabled={busy}
            className="app-btn-ghost"
            style={{ padding: "0.25rem 0.625rem", borderRadius: "0.5rem" }}
            title="Switch to a different cluster context"
          >
            Switch
          </button>
          <button onClick={handleDisconnect} disabled={busy} className="app-btn-ghost" style={{ padding: "0.25rem 0.625rem", borderRadius: "0.5rem" }}>
            Disconnect
          </button>
        </>
      ) : (
        <button onClick={() => setOpen(!open)} className="app-btn-ghost" style={{ padding: "0.25rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.75rem" }}>
          Aim at a cluster
        </button>
      )}

      {open && (
        <div
          style={{
            position: "absolute", left: trigger ? 0 : "auto", right: trigger ? "auto" : 0,
            top: trigger ? "calc(100% + 0.5rem)" : "2.25rem", zIndex: 50, width: "28rem",
            borderRadius: "1rem", boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)", padding: "1rem",
            display: "flex", flexDirection: "column", gap: "0.75rem",
            background: "var(--paper-2)", border: "1px solid var(--rule)"
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h3 style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--ink)", margin: 0 }}>
              {status?.connected ? "Aim at a different cluster" : "Aim at a cluster"}
            </h3>
            <button onClick={() => setOpen(false)} aria-label="Close" style={{ color: "var(--ink-3)", background: "none", border: "none", fontSize: "1.25rem", cursor: "pointer", padding: 0 }}>
              &times;
            </button>
          </div>

          {/* Disconnect lives here rather than in the header: it only exists
              when connected, and a control that appears and disappears from a
              toolbar makes every neighbour move. */}
          {trigger && status?.connected && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem", padding: "0.5rem 0.625rem", borderRadius: "0.5rem", background: "var(--brand-bg)", border: "1px solid var(--brand-bd)" }}>
              <span style={{ color: "var(--ink-2)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                Currently aimed at <strong style={{ color: "var(--ink)" }}>{status.context_name || status.cluster_name}</strong>
              </span>
              <button onClick={handleDisconnect} disabled={busy} className="app-btn-ghost" style={{ padding: "0.25rem 0.625rem", borderRadius: "0.5rem", flex: "none" }}>
                Disconnect
              </button>
            </div>
          )}

          <div style={{ display: "flex", gap: "0.5rem" }}>
            <button
              onClick={() => setMode("autodetect")}
              style={{
                borderRadius: "0.5rem", padding: "0.375rem 0.75rem", fontSize: "0.75rem", cursor: "pointer",
                background: mode === "autodetect" ? "var(--brand-bg)" : "var(--paper-3)",
                color: mode === "autodetect" ? "var(--brand)" : "var(--ink-2)",
                border: "1px solid var(--rule)",
              }}
            >
              Autodetect
            </button>
            <button
              onClick={() => setMode("kubeconfig-upload")}
              style={{
                borderRadius: "0.5rem", padding: "0.375rem 0.75rem", fontSize: "0.75rem", cursor: "pointer",
                background: mode === "kubeconfig-upload" ? "var(--brand-bg)" : "var(--paper-3)",
                color: mode === "kubeconfig-upload" ? "var(--brand)" : "var(--ink-2)",
                border: "1px solid var(--rule)",
              }}
            >
              Paste kubeconfig
            </button>
            {onUseSsh && (
              <button
                onClick={onUseSsh}
                style={{
                  borderRadius: "0.5rem", padding: "0.375rem 0.75rem", fontSize: "0.75rem", cursor: "pointer",
                  background: "var(--paper-3)", color: "var(--ink-2)", border: "1px solid var(--rule)",
                  marginLeft: "auto",
                }}
                title="Reach a cluster on another host over SSH"
              >
                Over SSH
              </button>
            )}
          </div>

          {mode === "kubeconfig-upload" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              <textarea
                name="kubeconfig-content"
                value={content}
                onChange={(event) => setContent(event.target.value)}
                placeholder="Paste kubeconfig YAML..."
                className="app-input"
                style={{ minHeight: "8rem", borderRadius: "0.75rem", padding: "0.5rem 0.75rem", fontSize: "0.75rem", fontFamily: "var(--mono)", resize: "vertical" }}
              />
              <button onClick={handleUpload} disabled={busy || !content.trim()} className="app-btn-primary" style={{ borderRadius: "0.5rem", padding: "0.5rem 0.75rem", fontSize: "0.75rem" }}>
                Parse kubeconfig
              </button>
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <label style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>
              Context
            </label>
            <select
              name="kube-context"
              value={currentContext ?? ""}
              onChange={(event) => setCurrentContext(event.target.value)}
              className="app-input"
              style={{ borderRadius: "0.5rem", padding: "0.5rem 0.75rem", fontSize: "0.875rem", width: "100%" }}
              disabled={busy || contexts.length === 0}
            >
              {contexts.length === 0 && <option value="">No contexts found</option>}
              {contexts.map((context) => (
                <option key={context.name} value={context.name}>
                  {context.name} ({context.namespace || "default"})
                </option>
              ))}
            </select>
          </div>

          {error && (
            <p style={{ borderRadius: "0.5rem", padding: "0.5rem 0.75rem", fontSize: "0.75rem", color: "var(--red)", border: "1px solid var(--red-bd)", margin: 0 }}>
              {error}
            </p>
          )}

          <button
            onClick={handleConnect}
            disabled={busy || !currentContext}
            className="app-btn-primary"
            style={{ borderRadius: "0.75rem", padding: "0.5rem 1rem", fontSize: "0.875rem", fontWeight: 500, marginTop: "0.25rem" }}
          >
            {busy ? "Working..." : "Connect"}
          </button>
        </div>
      )}
    </div>
  );
}
