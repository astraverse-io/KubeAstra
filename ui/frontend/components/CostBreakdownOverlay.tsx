import React, { useEffect, useState } from "react";
import { fetchAgentRunDetails, type AgentRunDetails } from "../lib/api";

export interface CostBreakdownOverlayProps {
  runId: string;
  onClose: () => void;
}

export function CostBreakdownOverlay({ runId, onClose }: CostBreakdownOverlayProps) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [details, setDetails] = useState<AgentRunDetails | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchAgentRunDetails(runId)
      .then((data) => {
        setDetails(data);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Failed to fetch run details:", err);
        setError(String(err));
        setLoading(false);
      });
  }, [runId]);

  const formatCost = (usd: number) => {
    if (usd === 0) return "$0.00";
    if (usd < 0.0001) return `$${usd.toFixed(6)}`;
    if (usd < 0.001) return `$${usd.toFixed(5)}`;
    return `$${usd.toFixed(4)}`;
  };

  const formatTokens = (count: number) => {
    if (count < 1000) return `${count}`;
    return `${(count / 1000).toFixed(1)}k`;
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 100,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.65)", backdropFilter: "blur(8px)",
      padding: "1rem"
    }}>
      <div style={{
        width: "600px", maxWidth: "100%", maxHeight: "88vh",
        background: "var(--paper-2)",
        border: "1px solid var(--rule)",
        borderRadius: "16px", overflow: "hidden",
        boxShadow: "0 32px 80px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.04), inset 0 1px 0 rgba(255,255,255,0.03)",
        display: "flex", flexDirection: "column",
      }}>
        {/* Header */}
        <div style={{
          padding: "14px 18px", background: "var(--paper-3)",
          borderBottom: "1px solid var(--rule)",
          display: "flex", alignItems: "center",
          justifyContent: "space-between", flexShrink: 0,
        }}>
          <div>
            <div style={{ fontSize: "14px", fontWeight: 600, color: "var(--ink)" }}>
              Run Spend Breakdown
            </div>
            <div style={{ fontSize: "10px", color: "var(--ink-3)", fontFamily: "var(--mono)", marginTop: "2px" }}>
              ID: {runId}
            </div>
          </div>
          <button onClick={onClose} style={{
            background: "none", border: "none", cursor: "pointer",
            color: "var(--ink-3)", padding: "4px", borderRadius: "5px", flexShrink: 0
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div style={{ flex: 1, overflowY: "auto", padding: "16px 18px" }}>
          {loading ? (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "3rem 0", gap: "1rem" }}>
              <div style={{
                width: "2rem", height: "2rem", border: "3px solid var(--rule)",
                borderTopColor: "var(--brand)", borderRadius: "50%",
                animation: "spin 1s linear infinite"
              }} />
              <style dangerouslySetInnerHTML={{__html: `
                @keyframes spin {
                  0% { transform: rotate(0deg); }
                  100% { transform: rotate(360deg); }
                }
              `}} />
              <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>Fetching run spend metrics...</div>
            </div>
          ) : error ? (
            <div style={{ padding: "1.5rem", borderRadius: "8px", background: "var(--red-bg)", border: "1px solid var(--red-bd)", color: "var(--red)", fontSize: "13px" }}>
              Failed to load cost breakdown details: {error}
            </div>
          ) : details ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
              
              {/* Summary Dashboard Card */}
              <div style={{
                padding: "16px", borderRadius: "10px", background: "var(--paper-3)", border: "1px solid var(--rule)",
                display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "12px"
              }}>
                <div>
                  <div style={{ fontSize: "10px", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>Total Cost</div>
                  <div style={{ fontSize: "20px", fontWeight: 700, color: "var(--brand)", marginTop: "4px" }}>
                    {formatCost(details.run.total_cost_usd)}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "10px", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>Total Tokens</div>
                  <div style={{ fontSize: "20px", fontWeight: 700, color: "var(--ink)", marginTop: "4px" }}>
                    {formatTokens(details.run.total_tokens_in + details.run.total_tokens_out)}
                  </div>
                </div>
                <div style={{ borderTop: "1px solid var(--rule)", paddingTop: "8px", marginTop: "4px" }}>
                  <div style={{ fontSize: "9px", color: "var(--ink-3)", textTransform: "uppercase" }}>Primary Model</div>
                  <div style={{ fontSize: "11px", fontWeight: 500, color: "var(--ink-2)", fontFamily: "var(--mono)", marginTop: "2px" }}>
                    {details.run.model || "unknown"}
                  </div>
                </div>
                <div style={{ borderTop: "1px solid var(--rule)", paddingTop: "8px", marginTop: "4px" }}>
                  <div style={{ fontSize: "9px", color: "var(--ink-3)", textTransform: "uppercase" }}>Cached Input Tokens</div>
                  <div style={{ fontSize: "11px", fontWeight: 500, color: "var(--ink-2)", marginTop: "2px" }}>
                    {formatTokens(details.run.total_cached_tokens_in)}
                  </div>
                </div>
              </div>

              {/* Steps Detailed Table */}
              <div>
                <div style={{ fontSize: "11px", color: "var(--ink-3)", letterSpacing: "0.1em", textTransform: "uppercase", fontWeight: 600, marginBottom: "8px" }}>
                  Step Details
                </div>
                
                {details.steps.length === 0 ? (
                  <div style={{ fontSize: "12px", color: "var(--ink-3)", fontStyle: "italic", textAlign: "center", padding: "1rem" }}>
                    No steps recorded for this run.
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                    {details.steps.map((step) => {
                      const totalTokens = step.tokens_in + step.tokens_out;
                      return (
                        <div key={step.id} style={{
                          padding: "10px 12px", borderRadius: "8px", background: "var(--paper)", border: "1px solid var(--rule)",
                          display: "flex", justifyContent: "space-between", alignItems: "center"
                        }}>
                          <div style={{ display: "flex", flexDirection: "column", gap: "3px", minWidth: 0 }}>
                            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                              <span style={{ fontSize: "9px", background: "var(--paper-3)", border: "1px solid var(--rule)", color: "var(--ink-3)", padding: "1px 4px", borderRadius: "4px", fontWeight: 600 }}>
                                #{step.iteration}
                              </span>
                              <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)" }}>
                                {step.action}
                              </span>
                              <span style={{ fontSize: "9px", color: "var(--ink-3)", background: "rgba(255,255,255,0.03)", border: "1px solid var(--rule)", padding: "1px 4px", borderRadius: "4px" }}>
                                {step.step_kind}
                              </span>
                            </div>
                            
                            <div style={{ fontSize: "10px", color: "var(--ink-3)", display: "flex", gap: "8px", flexWrap: "wrap" }}>
                              {step.step_model && (
                                <span style={{ fontFamily: "var(--mono)" }}>{step.step_model}</span>
                              )}
                              {totalTokens > 0 && (
                                <span>
                                  {formatTokens(totalTokens)} tokens (in: {formatTokens(step.tokens_in)}
                                  {step.cached_tokens_in > 0 && ` [${formatTokens(step.cached_tokens_in)} cached]`} · out: {formatTokens(step.tokens_out)})
                                </span>
                              )}
                              {step.duration_ms !== undefined && step.duration_ms > 0 && (
                                <span>{step.duration_ms}ms</span>
                              )}
                            </div>
                          </div>

                          <div style={{ textAlign: "right", flexShrink: 0, paddingLeft: "12px" }}>
                            <div style={{ fontSize: "13px", fontWeight: 600, color: step.cost_usd > 0 ? "var(--brand)" : "var(--ink-3)" }}>
                              {formatCost(step.cost_usd)}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
