import { useEffect, useState } from "react";
import { onHostMessage, ready } from "./webviewApi";

interface AuthState {
  signedIn: boolean;
  backendUrl: string | null;
  authRequired: boolean;
}

/**
 * M1 shell: proves the host↔webview bridge, CSP and theming work. The slim
 * Mission Control chat (reusing components from ui/frontend/components via the
 * Vite alias) lands in M3; the streaming chat pipeline in M2.
 */
export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [seed, setSeed] = useState("");

  useEffect(() => {
    const off = onHostMessage((msg) => {
      if (msg.type === "auth-state") {
        setAuth({
          signedIn: msg.signedIn,
          backendUrl: msg.backendUrl,
          authRequired: msg.authRequired,
        });
      } else if (msg.type === "prompt") {
        setSeed(msg.text);
      }
    });
    ready();
    return off;
  }, []);

  const status = !auth
    ? "Connecting…"
    : !auth.backendUrl
      ? "No backend configured — run “KubeAstra: Sign in”."
      : auth.signedIn
        ? `Connected to ${auth.backendUrl}`
        : `Sign-in required for ${auth.backendUrl}`;

  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
      <h3 style={{ margin: 0 }}>KubeAstra</h3>
      <p style={{ margin: 0, opacity: 0.8 }}>{status}</p>
      {seed && (
        <pre
          style={{
            whiteSpace: "pre-wrap",
            background: "var(--vscode-textCodeBlock-background)",
            padding: 8,
            borderRadius: 6,
            maxHeight: 200,
            overflow: "auto",
          }}
        >
          {seed}
        </pre>
      )}
    </div>
  );
}
