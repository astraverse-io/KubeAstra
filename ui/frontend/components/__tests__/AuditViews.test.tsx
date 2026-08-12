/**
 * The audit views.
 *
 * The route shape is the load-bearing test. `/audit/session/<id>` would be a
 * dynamic route, which cannot exist in the desktop static export — session
 * ids are not knowable at build time. That exact mistake shipped twice on the
 * alerts page: a 404 in 0.2.1, then a hang in 0.2.2. Replay is a query param
 * so one static page serves both builds.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

import AuditEventTable from "../AuditEventTable";
import AuditReplayTimeline from "../AuditReplayTimeline";
import type { AuditEvent } from "@/lib/api";

const ROOT = join(__dirname, "..", "..");

function event(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    seq: 1,
    id: "e1",
    ts: new Date().toISOString(),
    actor_type: "user",
    actor_id: "pruthvi@example.com",
    session_id: "demo-1",
    cluster: "prod",
    event_type: "mutation.executed",
    subject: "rollout_restart prod/api",
    payload: {},
    severity: "warn",
    hash: "h",
    prev_hash: null,
    ...overrides,
  };
}

describe("audit route shape", () => {
  it("replay is a query param, not a dynamic path segment", () => {
    const dynamic = join(ROOT, "app", "audit", "session");
    expect(
      existsSync(dynamic),
      "app/audit/session/[sessionId] is a dynamic route and cannot exist in " +
        "the desktop static export — use /audit?session=<id>",
    ).toBe(false);

    const page = readFileSync(join(ROOT, "app", "audit", "page.tsx"), "utf8");
    expect(page).toContain("?session=");
  });

  it("the audit route has no dynamic children at all", () => {
    const entries = readdirSync(join(ROOT, "app", "audit"), { withFileTypes: true });
    const dynamicDirs = entries.filter((e) => e.isDirectory() && e.name.includes("["));

    expect(dynamicDirs.map((d) => d.name)).toEqual([]);
  });

  it("reads the session without useSearchParams", () => {
    // useSearchParams forces a Suspense boundary under static export. The chat
    // page reads window.location directly for the same reason.
    const page = readFileSync(join(ROOT, "app", "audit", "page.tsx"), "utf8");
    // Match the import, not the word: the file explains in a comment why
    // useSearchParams is avoided, and a plain substring check flags that
    // comment as a violation.
    const code = page
      .split("\n")
      .filter((line) => !line.trim().startsWith("*") && !line.trim().startsWith("//"))
      .join("\n");

    expect(code).not.toMatch(/import[^;]*useSearchParams/);
    expect(code).not.toMatch(/useSearchParams\(/);
    expect(code).toContain("window.location.search");
  });

  it("handles browser back between the list and a replay", () => {
    const page = readFileSync(join(ROOT, "app", "audit", "page.tsx"), "utf8");

    expect(
      page,
      "without popstate, Back silently does nothing on a route that behaves " +
        "like two pages",
    ).toContain("popstate");
  });
});

describe("AuditEventTable", () => {
  it("renders an event and exposes its session", () => {
    const onSelect = vi.fn();
    render(<AuditEventTable events={[event()]} onSelectSession={onSelect} />);

    expect(screen.getByText("mutation.executed")).toBeTruthy();
    expect(screen.getByText(/rollout_restart prod\/api/)).toBeTruthy();

    fireEvent.click(screen.getByText(/demo-1/));
    expect(onSelect).toHaveBeenCalledWith("demo-1");
  });

  it("says so when nothing matches, rather than rendering an empty table", () => {
    render(<AuditEventTable events={[]} />);

    expect(screen.getByText(/No events match/)).toBeTruthy();
  });

  it("survives an event with no session or cluster", () => {
    // System events — a webhook arriving — have neither.
    render(
      <AuditEventTable
        events={[event({ session_id: null, cluster: null, subject: null })]}
      />,
    );

    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });
});

describe("AuditReplayTimeline", () => {
  it("keeps payload detail collapsed until asked", () => {
    // An expanded payload per row makes a twenty-step investigation
    // unreadable.
    render(
      <AuditReplayTimeline
        events={[event({ payload: { action: "rollout_restart", outcome: "executed" } })]}
      />,
    );

    expect(screen.queryByText(/"outcome"/)).toBeNull();

    fireEvent.click(screen.getByText(/detail/));
    expect(screen.getByText(/"outcome"/)).toBeTruthy();
  });

  it("does not offer a detail toggle when there is no payload", () => {
    render(<AuditReplayTimeline events={[event({ payload: {} })]} />);

    expect(screen.queryByText(/detail/)).toBeNull();
  });

  it("says nothing happened rather than rendering an empty list", () => {
    render(<AuditReplayTimeline events={[]} />);

    expect(screen.getByText(/Nothing auditable/)).toBeTruthy();
  });
});
