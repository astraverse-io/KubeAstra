"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchClusterSummary, type ClusterSummary } from "./api";

/** Matches the server-side cache TTL. Polling faster only returns the same
 *  cached object with a larger `cache_age_seconds`. */
export const POLL_INTERVAL_MS = 30_000;

export interface ClusterSummaryState {
  summary: ClusterSummary | null;
  /** True only for the first load. A background refresh must not blank the
   *  header — numbers that vanish every 30 seconds read as a broken panel. */
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useClusterSummary(
  sessionId: string | null | undefined,
): ClusterSummaryState {
  const [summary, setSummary] = useState<ClusterSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow response for an old session landing after the user
  // has switched to a new one and overwriting it.
  const currentSession = useRef<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(
    async (id: string, isFirst: boolean) => {
      if (isFirst) setLoading(true);
      try {
        const next = await fetchClusterSummary(id);
        if (!mounted.current || currentSession.current !== id) return;
        setSummary(next);
        setError(null);
      } catch (err) {
        if (!mounted.current || currentSession.current !== id) return;
        // Keep the last good summary on screen. A transient failure should
        // cost freshness, not the numbers themselves.
        setError(err instanceof Error ? err.message : "could not reach the cluster");
      } finally {
        if (mounted.current && currentSession.current === id) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    currentSession.current = sessionId ?? null;

    if (!sessionId) {
      setSummary(null);
      setError(null);
      setLoading(false);
      return;
    }

    // Clear immediately on a session change: showing the previous cluster's
    // pod count under a new cluster's name is worse than showing nothing.
    setSummary(null);
    void load(sessionId, true);

    const timer = setInterval(() => {
      // Polling a hidden tab burns a kubectl call per interval for a header
      // nobody is looking at.
      if (typeof document !== "undefined" && document.hidden) return;
      void load(sessionId, false);
    }, POLL_INTERVAL_MS);

    return () => clearInterval(timer);
  }, [sessionId, load]);

  const refresh = useCallback(() => {
    if (sessionId) void load(sessionId, false);
  }, [sessionId, load]);

  return { summary, loading, error, refresh };
}
