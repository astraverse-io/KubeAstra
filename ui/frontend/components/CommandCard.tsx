"use client";

import { useState } from "react";
import { Copy, Check, Terminal } from "lucide-react";
import { copyToClipboard } from "../lib/clipboard";

interface Command {
  cmd: string;
  description?: string;
}

interface Props {
  commands: Command[];
  title?: string;
}

export default function CommandCard({ commands, title = "Commands" }: Props) {
  if (!commands || commands.length === 0) return null;

  return (
    <div style={{ borderRadius: "0.5rem", border: "1px solid var(--rule)", overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", padding: "0.5rem 1rem", backgroundColor: "var(--paper-2)", borderBottom: "1px solid var(--rule)" }}>
        <Terminal size={14} color="var(--ink-3)" />
        <span style={{ fontSize: "0.75rem", fontWeight: 500, color: "var(--ink-2)" }}>{title}</span>
      </div>
      <div style={{ display: "flex", flexDirection: "column" }}>
        {commands.map((c, i) => (
          <div key={i} style={{ borderTop: i > 0 ? "1px solid var(--rule)" : "none" }}>
            <CommandRow cmd={c.cmd} description={c.description} />
          </div>
        ))}
      </div>
    </div>
  );
}

function CommandRow({ cmd, description }: { cmd: string; description?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const success = await copyToClipboard(cmd);
    if (success) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div style={{ padding: "0.75rem 1rem", backgroundColor: "var(--paper)" }}>
      {description && (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", marginBottom: "0.375rem", marginTop: 0 }}>{description}</p>
      )}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "0.75rem" }}>
        <code style={{ fontSize: "0.875rem", color: "var(--brand)", fontFamily: "var(--mono)", wordBreak: "break-all", lineHeight: 1.625 }}>
          {cmd}
        </code>
        <button
          onClick={handleCopy}
          style={{
            flexShrink: 0, marginTop: "0.125rem", padding: "0.25rem", borderRadius: "0.25rem",
            color: copied ? "var(--green)" : "var(--ink-3)", background: "none", border: "none", cursor: "pointer",
            transition: "color 0.15s"
          }}
          title="Copy command"
        >
          {copied ? (
            <Check size={14} />
          ) : (
            <Copy size={14} />
          )}
        </button>
      </div>
    </div>
  );
}
