import React, { useRef, useState, useEffect } from "react";

interface IntentBarProps {
  onSend: (text: string) => void;
  listening: boolean;
  contextName?: string;
  onStop?: () => void;
}

export function IntentBar({ onSend, listening, contextName, onStop }: IntentBarProps) {
  const [val, setVal] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    if (!val.trim() || listening) return;
    onSend(val.trim());
    setVal("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const autoResize = () => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  };

  useEffect(() => {
    autoResize();
  }, [val]);

  return (
    <div style={{
      padding: "12px 18px 14px",
      borderTop: "1px solid var(--rule)",
      background: "var(--paper-2)", flexShrink: 0,
    }}>
      <div style={{
        display: "flex", alignItems: "center",
        background: "var(--paper)",
        border: `1.5px solid ${listening ? "var(--brand)" : "var(--rule-2)"}`,
        borderRadius: "10px",
        boxShadow: listening ? `0 0 0 3px var(--brand-bd)` : "0 1px 4px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.02)",
        overflow: "hidden",
        transition: "border-color 0.25s, box-shadow 0.25s",
        maxWidth: "100%", margin: "0",
      }}>
        {/* Cluster badge (left) */}
        <div style={{
          padding: "0 12px", height: "100%", minHeight: "48px",
          display: "flex", alignItems: "center",
          borderRight: "1px solid var(--rule)",
          flexShrink: 0,
        }}>
          <span style={{
            fontSize: "10px", fontFamily: "var(--mono)", fontWeight: 600,
            color: "var(--brand)", letterSpacing: "0.06em",
            background: "var(--brand-bg)", border: "1px solid var(--brand-bd)",
            borderRadius: "4px", padding: "3px 8px",
            textTransform: "uppercase"
          }}>
            {contextName || "NO-CLUSTER"}
          </span>
        </div>

        {/* Input */}
        <textarea
          name="chat-message"
          ref={textareaRef}
          // See CommandBar: the desktop shell focuses whichever bar is mounted.
          data-kubeastra-input=""
          value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Ask anything about your cluster…"
          style={{
            flex: 1, padding: "13px 14px",
            background: "transparent", border: "none", outline: "none",
            color: "var(--ink)", fontSize: "13px", fontFamily: "var(--sans)",
            resize: "none", maxHeight: "200px", paddingTop: "22px"
          }}
          disabled={listening}
        />

        {/* Listening dots */}
        {listening && (
          <div style={{ padding: "0 10px", display: "flex", gap: "3px", alignItems: "center" }}>
            {[0, 1, 2].map(d => (
              <div key={d} style={{
                width: "4px", height: "4px", borderRadius: "50%", background: "var(--brand)",
                animation: `dotBounce 1.2s ${d * 0.2}s ease-in-out infinite`,
              }} />
            ))}
          </div>
        )}

        {/* Send / Stop */}
        {listening && onStop ? (
          <button
            onClick={onStop}
            style={{
              padding: "0 16px", height: "100%", minHeight: "48px",
              background: "rgba(220, 53, 69, 0.1)",
              border: "none", borderLeft: "1px solid var(--rule)",
              cursor: "pointer",
              color: "var(--danger, #dc3545)",
              transition: "all 0.2s",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
            title="Stop generation"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <rect x="3" y="3" width="18" height="18" rx="2" />
            </svg>
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!val.trim() || listening}
            style={{
              padding: "0 16px", height: "100%", minHeight: "48px",
              background: val.trim() && !listening ? "var(--brand-bg)" : "transparent",
              border: "none", borderLeft: "1px solid var(--rule)",
              cursor: val.trim() && !listening ? "pointer" : "default",
              color: val.trim() && !listening ? "var(--brand)" : "var(--ink-3)",
              transition: "all 0.2s",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
