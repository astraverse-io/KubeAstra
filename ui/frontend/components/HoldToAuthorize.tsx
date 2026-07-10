"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

type HoldToAuthorizeProps = {
  onConfirm: () => void;
  holdMs?: number;
  label?: string;
  confirmedLabel?: string;
  /**
   * When true, offer an Enter-key instant-confirm as accessible fallback.
   * Default true — hold gestures alone are inaccessible for keyboard-only
   * and screen-reader users, so this must stay on unless a caller has a
   * deliberate reason (which would need to provide its own equivalent).
   */
  keyboardFallback?: boolean;
};

/**
 * Mission-control arming gesture — hold to authorize a destructive action.
 *
 * Accessibility contract (do NOT remove):
 *   - Space bar hold mirrors the mouse/touch gesture for keyboard users.
 *   - Enter key confirms instantly (equivalent primary action).
 *   - aria-live announcements report progress at 25 / 50 / 75 / 100%.
 *   - A visible "or press Enter to confirm" hint accompanies the button.
 *   - focus-visible outline uses --cyan for high visibility on dark bg.
 */
export function HoldToAuthorize({
  onConfirm,
  holdMs = 1800,
  label = "HOLD TO EXECUTE",
  confirmedLabel = "DISPATCHED",
  keyboardFallback = true,
}: HoldToAuthorizeProps) {
  const [holding, setHolding] = useState(false);
  const [progress, setProgress] = useState(0);
  const [armed, setArmed] = useState(false);
  const [announced, setAnnounced] = useState("");
  const rafRef = useRef<number>(0);
  const startRef = useRef(0);
  const progressRef = useRef(0);
  const decayRef = useRef<number>(0);
  const spaceHeldRef = useRef(false);
  const announcedRef = useRef("");

  const stopAllTicks = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    if (decayRef.current) cancelAnimationFrame(decayRef.current);
  }, []);

  const announceForProgress = useCallback((p: number) => {
    const pct = Math.round(p * 100);
    let next = "";
    if (pct >= 100) next = "Authorization complete. Dispatching.";
    else if (pct >= 75) next = "75 percent — keep holding.";
    else if (pct >= 50) next = "50 percent";
    else if (pct >= 25) next = "25 percent";
    if (next && announcedRef.current !== next) {
      announcedRef.current = next;
      setAnnounced(next);
    }
  }, []);

  const begin = useCallback(() => {
    if (armed) return;
    stopAllTicks();
    setHolding(true);
    startRef.current = performance.now() - progressRef.current * holdMs;
    const tick = (t: number) => {
      const elapsed = t - startRef.current;
      const p = Math.min(elapsed / holdMs, 1);
      progressRef.current = p;
      setProgress(p);
      announceForProgress(p);
      if (p >= 1) {
        setArmed(true);
        setHolding(false);
        setTimeout(onConfirm, 600);
      } else {
        rafRef.current = requestAnimationFrame(tick);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [announceForProgress, armed, holdMs, onConfirm, stopAllTicks]);

  const end = useCallback(() => {
    if (armed) return;
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    setHolding(false);
    const startP = progressRef.current;
    const t0 = performance.now();
    const decay = (t: number) => {
      const e = t - t0;
      const p = Math.max(startP - e / 400, 0);
      setProgress(p);
      progressRef.current = p;
      if (p > 0) decayRef.current = requestAnimationFrame(decay);
    };
    decayRef.current = requestAnimationFrame(decay);
  }, [armed]);

  useEffect(() => () => stopAllTicks(), [stopAllTicks]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      if (armed || !keyboardFallback) return;
      if (e.key === "Enter") {
        e.preventDefault();
        setArmed(true);
        setProgress(1);
        progressRef.current = 1;
        const msg = "Authorization confirmed via keyboard. Dispatching.";
        announcedRef.current = msg;
        setAnnounced(msg);
        setTimeout(onConfirm, 400);
        return;
      }
      if (e.key === " " && !spaceHeldRef.current) {
        e.preventDefault();
        spaceHeldRef.current = true;
        begin();
      }
    },
    [armed, begin, keyboardFallback, onConfirm],
  );

  const onKeyUp = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      if (e.key === " ") {
        spaceHeldRef.current = false;
        end();
      }
    },
    [end],
  );

  const pct = Math.round(progress * 100);

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.10em",
            color: armed ? "var(--green)" : "var(--cyan, var(--brand))",
          }}
        >
          {armed ? "✓ Authorized · dispatching" : "Press & hold to authorize"}
        </span>
        <span
          style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3, var(--fg-3))" }}
        >
          {armed ? "COMPLETE" : holding ? `${pct}%` : "IDLE"}
        </span>
      </div>
      <button
        type="button"
        onMouseDown={begin}
        onMouseUp={end}
        onMouseLeave={end}
        onTouchStart={begin}
        onTouchEnd={end}
        onTouchCancel={end}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        disabled={armed}
        aria-label={armed ? "Authorized" : "Hold Space or press Enter to authorize"}
        aria-pressed={holding}
        style={{
          position: "relative",
          width: "100%",
          height: 56,
          background: armed ? "var(--green-bg)" : "var(--bg-0, var(--paper))",
          border: `1px solid ${armed ? "var(--green)" : holding ? "var(--cyan, var(--brand))" : "var(--line-2, var(--rule-2))"}`,
          borderRadius: 6,
          cursor: armed ? "default" : "pointer",
          overflow: "hidden",
          transition: "border-color 0.2s, background 0.2s",
          boxShadow: holding ? "0 0 24px rgba(94,234,212,0.2)" : "none",
          padding: 0,
        }}
      >
        <span
          aria-hidden="true"
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: `${progress * 100}%`,
            background: armed
              ? "linear-gradient(90deg, var(--green) 0%, rgba(52,211,153,0.5) 100%)"
              : "linear-gradient(90deg, var(--cyan-3, var(--brand-bright)) 0%, var(--cyan, var(--brand)) 100%)",
            transition: holding ? "none" : "width 0.2s",
            opacity: armed ? 0.4 : 0.6,
          }}
        />
        {holding && (
          <span
            aria-hidden="true"
            style={{
              position: "absolute",
              left: `${progress * 100}%`,
              top: 0,
              bottom: 0,
              width: 2,
              background: "var(--cyan, var(--brand))",
              boxShadow: "0 0 16px var(--cyan, var(--brand))",
            }}
          />
        )}
        <span
          style={{
            position: "relative",
            height: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 10,
          }}
        >
          {!armed ? (
            <>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--cyan, var(--brand))"
                strokeWidth={2}
                strokeLinecap="round"
                aria-hidden="true"
              >
                <rect x="3" y="11" width="18" height="11" rx="2" />
                <path d="M7 11V7a5 5 0 0110 0v4" />
              </svg>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 13,
                  fontWeight: 600,
                  color: holding ? "var(--cyan, var(--brand))" : "var(--ink, var(--fg-0))",
                  letterSpacing: "0.12em",
                }}
              >
                {label}
              </span>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 11,
                  color: "var(--ink-3, var(--fg-3))",
                }}
              >
                — {(holdMs / 1000).toFixed(1)}s arming —
              </span>
            </>
          ) : (
            <>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--green)"
                strokeWidth={2.5}
                strokeLinecap="round"
                aria-hidden="true"
              >
                <path d="M20 6L9 17l-5-5" />
              </svg>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 13,
                  fontWeight: 600,
                  color: "var(--green)",
                  letterSpacing: "0.12em",
                }}
              >
                {confirmedLabel}
              </span>
            </>
          )}
        </span>
      </button>
      {keyboardFallback && !armed && (
        <div
          style={{
            marginTop: 8,
            fontFamily: "var(--mono)",
            fontSize: 10,
            color: "var(--ink-3, var(--fg-3))",
            textAlign: "center",
            letterSpacing: "0.04em",
          }}
        >
          Keyboard: hold <kbd style={kbdStyle}>Space</kbd> or press <kbd style={kbdStyle}>Enter</kbd>
        </div>
      )}
      <span
        role="status"
        aria-live="polite"
        style={{
          position: "absolute",
          width: 1,
          height: 1,
          padding: 0,
          margin: -1,
          overflow: "hidden",
          clip: "rect(0,0,0,0)",
          whiteSpace: "nowrap",
          border: 0,
        }}
      >
        {announced}
      </span>
    </div>
  );
}

const kbdStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 5px",
  border: "1px solid var(--line-2, var(--rule-2))",
  borderBottomWidth: 2,
  borderRadius: 3,
  background: "var(--bg-2, var(--paper-3))",
  fontFamily: "var(--mono)",
  fontSize: 10,
  color: "var(--ink-2, var(--fg-2))",
  margin: "0 2px",
};

export default HoldToAuthorize;
