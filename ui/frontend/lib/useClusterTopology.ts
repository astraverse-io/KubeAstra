"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchClusterTopology, type ClusterTopology } from "./api";

/** Matches the server-side cache TTL, as with the header counters. */
export const TOPOLOGY_POLL_MS = 30_000;

export interface ClusterTopologyState {
  topology: ClusterTopology | null;
  loading: boolean;
  error: string | null;
}

export function useClusterTopology(
  sessionId: string | null | undefined,
  scope: "all" | "alerting" = "alerting",
  enabled = true,
): ClusterTopologyState {
  const [topology, setTopology] = useState<ClusterTopology | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentKey = useRef<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (key: string, id: string, s: string, isFirst: boolean) => {
    if (isFirst) setLoading(true);
    try {
      const next = await fetchClusterTopology(id, s as "all" | "alerting");
      if (!mounted.current || currentKey.current !== key) return;
      setTopology(next);
      setError(null);
    } catch (err) {
      if (!mounted.current || currentKey.current !== key) return;
      setError(err instanceof Error ? err.message : "could not read the cluster");
    } finally {
      if (mounted.current && currentKey.current === key) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const key = `${sessionId ?? ""}:${scope}`;
    currentKey.current = key;

    // `enabled` is how the collapsed accordion avoids paying for a view
    // nobody has opened — this is a second kubectl call per poll.
    if (!sessionId || !enabled) {
      setLoading(false);
      return;
    }

    setTopology(null);
    void load(key, sessionId, scope, true);

    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void load(key, sessionId, scope, false);
    }, TOPOLOGY_POLL_MS);

    return () => clearInterval(timer);
  }, [sessionId, scope, enabled, load]);

  return { topology, loading, error };
}
