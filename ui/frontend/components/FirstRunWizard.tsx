"use client";

import { useState } from "react";
import {
  setupDesktopEmbeddings,
  setupDesktopLlm,
  type DesktopSetupState,
} from "../lib/api";

interface FirstRunWizardProps {
  state: DesktopSetupState;
  onComplete: () => void;
}

type Provider = "anthropic" | "openai" | "gemini" | "ollama";

const PROVIDERS: {
  id: Provider;
  name: string;
  blurb: string;
  needsKey: boolean;
  keyHint: string;
}[] = [
  {
    id: "anthropic",
    name: "Anthropic",
    blurb: "Claude — strongest reasoning for multi-step investigations.",
    needsKey: true,
    keyHint: "sk-ant-…",
  },
  {
    id: "openai",
    name: "OpenAI",
    blurb: "GPT models. One key also covers investigation memory.",
    needsKey: true,
    keyHint: "sk-…",
  },
  {
    id: "gemini",
    name: "Google Gemini",
    blurb: "Fast and inexpensive. One key also covers memory.",
    needsKey: true,
    keyHint: "AIza…",
  },
  {
    id: "ollama",
    name: "Ollama",
    blurb: "Runs models on this machine. No key, no data leaves your laptop.",
    needsKey: false,
    keyHint: "",
  },
];

// Anthropic publishes no embeddings API, so semantic memory needs a second
// key from a provider that does. Skipping is allowed — memory falls back to
// keyword matching rather than disappearing.
const EMBEDDING_PROVIDERS = [
  { id: "voyage", name: "Voyage AI", keyHint: "pa-…" },
  { id: "openai", name: "OpenAI", keyHint: "sk-…" },
  { id: "gemini", name: "Google Gemini", keyHint: "AIza…" },
];

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export default function FirstRunWizard({ state, onComplete }: FirstRunWizardProps) {
  const [step, setStep] = useState<"provider" | "key" | "embeddings">("provider");
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [embeddingProvider, setEmbeddingProvider] = useState("voyage");
  const [embeddingKey, setEmbeddingKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selected = PROVIDERS.find((entry) => entry.id === provider)!;

  const handleProviderNext = () => {
    setError("");
    setStep("key");
  };

  const handleVerifyKey = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await setupDesktopLlm({
        provider,
        api_key: selected.needsKey ? apiKey.trim() : undefined,
      });
      if (result.needs_embeddings_key) {
        setStep("embeddings");
      } else {
        onComplete();
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const handleVerifyEmbeddings = async () => {
    setBusy(true);
    setError("");
    try {
      await setupDesktopEmbeddings({
        provider: embeddingProvider,
        api_key: embeddingKey.trim(),
      });
      onComplete();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="wizard-title"
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
          maxWidth: "34rem",
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
        <header style={{ display: "flex", flexDirection: "column", gap: "0.375rem" }}>
          <h2
            id="wizard-title"
            style={{ margin: 0, fontSize: "1.25rem", color: "var(--fg-0)" }}
          >
            {step === "embeddings" ? "Investigation memory" : "Welcome to KubeAstra"}
          </h2>
          <p style={{ margin: 0, fontSize: "0.8125rem", color: "var(--fg-2)" }}>
            {step === "provider" &&
              "Choose the AI provider to power investigations. You use your own key — KubeAstra never bills you for AI."}
            {step === "key" &&
              (selected.needsKey
                ? `Paste your ${selected.name} API key. It is stored in your operating system's keychain and never leaves this machine except to call ${selected.name}.`
                : `KubeAstra will look for Ollama on this machine.`)}
            {step === "embeddings" &&
              "Anthropic has no embeddings API, so semantic recall needs a key from another provider. You can skip this — memory still works, matching on keywords instead."}
          </p>
        </header>

        {!state.keychain_secure && (
          <p
            role="status"
            style={{
              margin: 0,
              padding: "0.625rem 0.75rem",
              borderRadius: "0.5rem",
              border: "1px solid var(--amber-bd, var(--line))",
              background: "var(--amber-bg, var(--bg-2))",
              fontSize: "0.75rem",
              color: "var(--fg-1)",
            }}
          >
            No system keychain is available here ({state.keychain_backend}). Keys
            will be saved to a file readable only by your user account, which is
            less protected than the keychain.
          </p>
        )}

        {step === "provider" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {PROVIDERS.map((entry) => {
              const active = entry.id === provider;
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setProvider(entry.id)}
                  aria-pressed={active}
                  style={{
                    textAlign: "left",
                    padding: "0.75rem 0.875rem",
                    borderRadius: "0.625rem",
                    cursor: "pointer",
                    border: `1px solid ${active ? "var(--cyan-1, var(--line-3))" : "var(--line)"}`,
                    background: active ? "var(--bg-3)" : "var(--bg-2)",
                    color: "var(--fg-0)",
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: "0.875rem" }}>{entry.name}</div>
                  <div style={{ fontSize: "0.75rem", color: "var(--fg-2)", marginTop: "0.1875rem" }}>
                    {entry.blurb}
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {step === "key" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {selected.needsKey ? (
              <>
                <label htmlFor="wizard-api-key" style={{ fontSize: "0.75rem", color: "var(--fg-2)" }}>
                  {selected.name} API key
                </label>
                <input
                  id="wizard-api-key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={selected.keyHint}
                  className="sym-input"
                  style={{
                    borderRadius: "0.5rem",
                    padding: "0.5rem 0.75rem",
                    fontSize: "0.875rem",
                    fontFamily: "var(--mono)",
                  }}
                />
              </>
            ) : (
              <p style={{ margin: 0, fontSize: "0.8125rem", color: "var(--fg-2)" }}>
                Make sure Ollama is running, then continue. If it is not
                installed, get it from ollama.com and run <code>ollama serve</code>.
              </p>
            )}
          </div>
        )}

        {step === "embeddings" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            <label htmlFor="wizard-embed-provider" style={{ fontSize: "0.75rem", color: "var(--fg-2)" }}>
              Embeddings provider
            </label>
            <select
              id="wizard-embed-provider"
              value={embeddingProvider}
              onChange={(event) => setEmbeddingProvider(event.target.value)}
              className="sym-input"
              style={{ borderRadius: "0.5rem", padding: "0.5rem 0.75rem", fontSize: "0.875rem" }}
            >
              {EMBEDDING_PROVIDERS.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.name}
                </option>
              ))}
            </select>
            <label htmlFor="wizard-embed-key" style={{ fontSize: "0.75rem", color: "var(--fg-2)" }}>
              API key
            </label>
            <input
              id="wizard-embed-key"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={embeddingKey}
              onChange={(event) => setEmbeddingKey(event.target.value)}
              placeholder={
                EMBEDDING_PROVIDERS.find((e) => e.id === embeddingProvider)?.keyHint
              }
              className="sym-input"
              style={{
                borderRadius: "0.5rem",
                padding: "0.5rem 0.75rem",
                fontSize: "0.875rem",
                fontFamily: "var(--mono)",
              }}
            />
          </div>
        )}

        {error && (
          <p
            role="alert"
            style={{
              margin: 0,
              padding: "0.625rem 0.75rem",
              borderRadius: "0.5rem",
              border: "1px solid var(--red-bd, var(--line))",
              fontSize: "0.75rem",
              color: "var(--red, var(--fg-0))",
              wordBreak: "break-word",
            }}
          >
            {error}
          </p>
        )}

        <footer style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
          {step === "key" && (
            <button
              type="button"
              onClick={() => {
                setError("");
                setStep("provider");
              }}
              disabled={busy}
              className="sym-btn-ghost"
              style={{ borderRadius: "0.5rem", padding: "0.5rem 0.875rem", fontSize: "0.8125rem" }}
            >
              Back
            </button>
          )}
          {step === "embeddings" && (
            <button
              type="button"
              onClick={onComplete}
              disabled={busy}
              className="sym-btn-ghost"
              style={{ borderRadius: "0.5rem", padding: "0.5rem 0.875rem", fontSize: "0.8125rem" }}
            >
              Skip — use keyword memory
            </button>
          )}
          <button
            type="button"
            onClick={
              step === "provider"
                ? handleProviderNext
                : step === "key"
                  ? handleVerifyKey
                  : handleVerifyEmbeddings
            }
            disabled={
              busy ||
              (step === "key" && selected.needsKey && !apiKey.trim()) ||
              (step === "embeddings" && !embeddingKey.trim())
            }
            className="sym-btn-primary"
            style={{ borderRadius: "0.5rem", padding: "0.5rem 1rem", fontSize: "0.8125rem", fontWeight: 500 }}
          >
            {busy
              ? "Checking…"
              : step === "provider"
                ? "Continue"
                : step === "key"
                  ? "Test connection"
                  : "Enable memory"}
          </button>
        </footer>
      </div>
    </div>
  );
}
