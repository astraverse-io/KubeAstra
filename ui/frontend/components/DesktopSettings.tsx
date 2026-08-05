"use client";

/**
 * Desktop settings.
 *
 * Exists because five capabilities were reachable only by curl, and two
 * states had no way out at all:
 *
 *   - A rotated or revoked API key bricked the app. `configured` is sticky, so
 *     the first-run wizard never returned and nothing else could clear a key.
 *     "Forget this key" is the escape hatch.
 *   - The insecure-keychain warning appeared once, in the wizard. If the
 *     keychain degraded afterwards, keys silently fell back to a 0600 file
 *     with nothing on screen saying so.
 *
 * Everything here talks to endpoints that already existed and were already
 * typed in lib/api.ts — this screen is the missing surface, not new backend.
 */

import { useCallback, useEffect, useState } from "react";
import {
  fetchDesktopSettings,
  fetchDesktopSetup,
  forgetDesktopSecret,
  setupDesktopEmbeddings,
  testAlertmanager,
  updateDesktopSettings,
  type DesktopSettingsState,
  type DesktopSetupState,
} from "../lib/api";

interface DesktopSettingsProps {
  onClose: () => void;
  /** Called after a credential is forgotten, so the parent can re-run setup. */
  onCredentialCleared: () => void;
}

type Busy = null | "saving" | "testing" | "forgetting" | "embeddings";

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Google Gemini",
  ollama: "Ollama (local)",
};

export function DesktopSettings({ onClose, onCredentialCleared }: DesktopSettingsProps) {
  const [settings, setSettings] = useState<DesktopSettingsState | null>(null);
  const [setup, setSetup] = useState<DesktopSetupState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [alertUrl, setAlertUrl] = useState("");
  const [confirmForget, setConfirmForget] = useState(false);
  const [embProvider, setEmbProvider] = useState("voyage");
  const [embKey, setEmbKey] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, u] = await Promise.all([fetchDesktopSettings(), fetchDesktopSetup()]);
      setSettings(s);
      setSetup(u);
      setAlertUrl(s.alertmanager_url);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Escape closes, as in any settings dialog.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function save(payload: Parameters<typeof updateDesktopSettings>[0]) {
    setBusy("saving");
    setError(null);
    setNotice(null);
    try {
      setSettings(await updateDesktopSettings(payload));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  // Verify before storing, the same rule the wizard follows for API keys:
  // polling runs on a background thread, so a bad URL would otherwise fail
  // silently where nobody is looking.
  async function saveAlertmanager() {
    setBusy("testing");
    setError(null);
    setNotice(null);
    try {
      const result = await testAlertmanager(alertUrl);
      await save({ alertmanager_url: alertUrl });
      setNotice(
        `Reached Alertmanager — ${result.firing} alert${result.firing === 1 ? "" : "s"} firing.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  // Verified against the provider before storing, same rule as the LLM key:
  // an unusable embeddings key shows up later as memory silently staying in
  // keyword mode, which reads as "memory is broken" rather than "bad key".
  async function saveEmbeddingsKey() {
    setBusy("embeddings");
    setError(null);
    setNotice(null);
    try {
      const result = await setupDesktopEmbeddings({
        provider: embProvider,
        api_key: embKey.trim(),
      });
      setEmbKey("");
      setNotice(`Embeddings key stored — ${result.provider}, ${result.dim} dimensions.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function forgetLlmKey() {
    if (!setup?.llm_provider) return;
    setBusy("forgetting");
    setError(null);
    try {
      await forgetDesktopSecret(`llm.${setup.llm_provider}`);
      setConfirmForget(false);
      onCredentialCleared();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(null);
    }
  }

  const disabled = busy !== null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.6)",
        padding: "1rem",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: "38rem",
          maxHeight: "90vh",
          overflowY: "auto",
          borderRadius: "1rem",
          border: "1px solid var(--line)",
          background: "var(--bg-1)",
          padding: "1.75rem",
          display: "flex",
          flexDirection: "column",
          gap: "1.25rem",
        }}
      >
        <header
          style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "1rem" }}
        >
          <div>
            <h2 id="settings-title" style={{ margin: 0, fontSize: "1.25rem", color: "var(--fg-0)" }}>
              Settings
            </h2>
            <p style={{ margin: "0.375rem 0 0", fontSize: "0.8125rem", color: "var(--fg-2)" }}>
              Everything here is stored on this machine. API keys live in the OS keychain.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close settings" style={ghostButton}>
            ✕
          </button>
        </header>

        {error && <Banner tone="error">{error}</Banner>}
        {notice && <Banner tone="ok">{notice}</Banner>}

        {!settings || !setup ? (
          <p style={{ margin: 0, fontSize: "0.8125rem", color: "var(--fg-2)" }}>Loading…</p>
        ) : (
          <>
            {!settings.keychain_secure && (
              <Banner tone="warn">
                No system keychain is available here ({settings.keychain_backend || "unknown"}).
                Keys are saved to a file readable only by your user account, which is less
                protected than the keychain.
              </Banner>
            )}

            <Section
              title="AI provider"
              hint="Investigations use your own key. KubeAstra never bills you for AI."
            >
              <Row
                label="Provider"
                value={
                  setup.llm_provider
                    ? PROVIDER_LABEL[setup.llm_provider] ?? setup.llm_provider
                    : "Not configured"
                }
              />
              {setup.llm_provider && setup.llm_provider !== "ollama" && (
                <>
                  <p style={hintText}>
                    Rotating or revoking a key used to leave no way back into setup. Forgetting
                    it here returns the app to the first-run wizard.
                  </p>
                  {confirmForget ? (
                    <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
                      <span style={{ ...hintText, margin: 0 }}>
                        Forget the {PROVIDER_LABEL[setup.llm_provider] ?? setup.llm_provider} key?
                      </span>
                      <button type="button" onClick={forgetLlmKey} disabled={disabled} style={dangerButton}>
                        {busy === "forgetting" ? "Forgetting…" : "Yes, forget it"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmForget(false)}
                        disabled={disabled}
                        style={ghostButton}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setConfirmForget(true)}
                      disabled={disabled}
                      style={secondaryButton}
                    >
                      Forget this key
                    </button>
                  )}
                </>
              )}
            </Section>

            <Section
              title="Cluster"
              hint="Which cluster background investigations run against, and the kubectl doing the work."
            >
              <Row
                label="Alert investigations target"
                value={settings.default_cluster_context || "No cluster chosen"}
              />
              {!settings.default_cluster_context && (
                <p style={hintText}>
                  Connect a cluster to set this. Until then, alert-driven investigations refuse
                  to run rather than fall back to whatever this machine&apos;s kubeconfig points at.
                </p>
              )}
              <Row label="kubectl" value={settings.kubectl_path || "Not found"} mono />
              {settings.missing_auth_plugins.length > 0 && (
                <p style={hintText}>
                  Credential plugins not found: {settings.missing_auth_plugins.join(", ")}. These
                  only matter for clusters whose kubeconfig uses them (GKE, EKS, AKS).
                </p>
              )}
            </Section>

            <Section
              title="Notifications"
              hint="Poll Alertmanager and raise a desktop notification when something fires."
            >
              <label style={labelText} htmlFor="alertmanager-url">
                Alertmanager URL
              </label>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <input
                  id="alertmanager-url"
                  type="url"
                  value={alertUrl}
                  onChange={(event) => setAlertUrl(event.target.value)}
                  placeholder="http://localhost:9093"
                  disabled={disabled}
                  style={inputStyle}
                />
                <button type="button" onClick={saveAlertmanager} disabled={disabled} style={secondaryButton}>
                  {busy === "testing" ? "Checking…" : "Test & save"}
                </button>
              </div>
              <Toggle
                label="Enable notifications"
                checked={settings.notifications_enabled}
                disabled={disabled || !settings.alertmanager_url}
                onChange={(next) => save({ notifications_enabled: next })}
                hint={
                  settings.alertmanager_url
                    ? undefined
                    : "Set an Alertmanager URL first — notifications with nowhere to poll would do nothing."
                }
              />
            </Section>

            <Section
              title="Investigation memory"
              hint="Past investigations inform new ones. Semantic recall uses your provider's embeddings — Ollama needs `ollama pull nomic-embed-text`, Claude needs a separate embeddings key. Without one it matches on keywords."
            >
              <Row
                label="Mode"
                value={settings.memory_mode === "vector" ? "Semantic (vector)" : "Keyword only"}
              />
              <Toggle
                label="Use investigation memory"
                checked={settings.memory_enabled}
                disabled={disabled}
                onChange={(next) => save({ memory_enabled: next })}
              />

              <label style={labelText} htmlFor="embeddings-provider">
                Embeddings key
              </label>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <select
                  id="embeddings-provider"
                  value={embProvider}
                  onChange={(event) => setEmbProvider(event.target.value)}
                  disabled={disabled}
                  style={{ ...inputStyle, flex: "0 0 auto" }}
                >
                  <option value="voyage">Voyage</option>
                  <option value="openai">OpenAI</option>
                  <option value="gemini">Gemini</option>
                </select>
                <input
                  id="embeddings-key"
                  type="password"
                  value={embKey}
                  onChange={(event) => setEmbKey(event.target.value)}
                  placeholder="Paste a key to enable semantic recall"
                  aria-label="Embeddings API key"
                  disabled={disabled}
                  style={inputStyle}
                />
                <button
                  type="button"
                  onClick={saveEmbeddingsKey}
                  disabled={disabled || !embKey.trim()}
                  style={secondaryButton}
                >
                  {busy === "embeddings" ? "Checking…" : "Save key"}
                </button>
              </div>
              <p style={hintText}>
                Verified against the provider before it is stored — an unusable key would
                otherwise surface much later, as memory quietly staying in keyword mode.
              </p>
            </Section>

            <Section title="Diagnostics" hint="Off by default. Nothing leaves this machine unless you turn it on.">
              <Toggle
                label="Allow remote diagnostics"
                checked={settings.remote_diagnostics_enabled}
                disabled={disabled}
                onChange={(next) => save({ remote_diagnostics_enabled: next })}
              />
            </Section>
          </>
        )}
      </div>
    </div>
  );
}

/* ── small presentational helpers ─────────────────────────────── */

const hintText: React.CSSProperties = {
  margin: 0,
  fontSize: "0.75rem",
  color: "var(--fg-2)",
  lineHeight: 1.5,
};

const labelText: React.CSSProperties = {
  fontSize: "0.75rem",
  color: "var(--fg-1)",
};

const inputStyle: React.CSSProperties = {
  flex: "1 1 14rem",
  minWidth: 0,
  padding: "0.5rem 0.625rem",
  borderRadius: "0.5rem",
  border: "1px solid var(--line)",
  background: "var(--bg-2)",
  color: "var(--fg-0)",
  fontSize: "0.8125rem",
};

const baseButton: React.CSSProperties = {
  padding: "0.5rem 0.75rem",
  borderRadius: "0.5rem",
  fontSize: "0.8125rem",
  cursor: "pointer",
  border: "1px solid var(--line)",
};

const secondaryButton: React.CSSProperties = {
  ...baseButton,
  background: "var(--bg-2)",
  color: "var(--fg-0)",
};

const ghostButton: React.CSSProperties = {
  ...baseButton,
  background: "transparent",
  color: "var(--fg-2)",
};

const dangerButton: React.CSSProperties = {
  ...baseButton,
  background: "var(--red-bg, var(--bg-2))",
  color: "var(--red, var(--fg-0))",
  borderColor: "var(--red-bd, var(--line))",
};

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <h3 style={{ margin: 0, fontSize: "0.9375rem", color: "var(--fg-0)" }}>{title}</h3>
      {hint && <p style={hintText}>{hint}</p>}
      {children}
    </section>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: "1rem",
        fontSize: "0.8125rem",
        padding: "0.375rem 0",
      }}
    >
      <span style={{ color: "var(--fg-2)" }}>{label}</span>
      <span
        style={{
          color: "var(--fg-0)",
          fontFamily: mono ? "var(--mono, monospace)" : undefined,
          wordBreak: "break-all",
          textAlign: "right",
        }}
      >
        {value}
      </span>
    </div>
  );
}

function Toggle({
  label,
  checked,
  disabled,
  onChange,
  hint,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
  hint?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      <label style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.8125rem" }}>
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span style={{ color: "var(--fg-0)" }}>{label}</span>
      </label>
      {hint && <p style={{ ...hintText, paddingLeft: "1.5rem" }}>{hint}</p>}
    </div>
  );
}

function Banner({ tone, children }: { tone: "error" | "warn" | "ok"; children: React.ReactNode }) {
  const palette = {
    error: { bd: "var(--red-bd, var(--line))", bg: "var(--red-bg, var(--bg-2))" },
    warn: { bd: "var(--amber-bd, var(--line))", bg: "var(--amber-bg, var(--bg-2))" },
    ok: { bd: "var(--green-bd, var(--line))", bg: "var(--green-bg, var(--bg-2))" },
  }[tone];
  return (
    <p
      role="status"
      style={{
        margin: 0,
        padding: "0.625rem 0.75rem",
        borderRadius: "0.5rem",
        border: `1px solid ${palette.bd}`,
        background: palette.bg,
        fontSize: "0.75rem",
        color: "var(--fg-1)",
      }}
    >
      {children}
    </p>
  );
}

export default DesktopSettings;
