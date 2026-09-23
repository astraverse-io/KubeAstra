import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError, type ProposedWrite } from "../../lib/api";

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    getPendingWrite: vi.fn(),
    applyPendingWrite: vi.fn(),
    discardPendingWrite: vi.fn(),
  };
});

import * as api from "../../lib/api";
import WriteApprovalCard from "../WriteApprovalCard";

const SANITIZED = "--- a/prod/api.yaml\n+++ b/prod/api.yaml\n@@\n   token: ***redacted***\n-  replicas: 2\n+  replicas: 4\n";
const REAL = "--- a/prod/api.yaml\n+++ b/prod/api.yaml\n@@\n   token: ghp_realcontext\n-  replicas: 2\n+  replicas: 4\n";

function proposal(over: Partial<ProposedWrite> = {}): ProposedWrite {
  return {
    token: "pwr_abc",
    path: "prod/api.yaml",
    root: "/Users/me/infra",
    created: false,
    reason: "raise replicas to absorb load",
    diff: SANITIZED,
    validation: {
      ok: true,
      validated: false,
      unvalidated_reason: "kubeconform: kubeconform not installed",
      checks: [
        { name: "yaml_parse", status: "pass", detail: "" },
        { name: "policy", status: "warn", detail: "pre-existing / advisory: hostPath volume h" },
        { name: "kubeconform", status: "skipped", detail: "kubeconform not installed" },
      ],
    },
    expires_at: 1,
    ...over,
  };
}

const getPending = vi.mocked(api.getPendingWrite);
const apply = vi.mocked(api.applyPendingWrite);
const discard = vi.mocked(api.discardPendingWrite);

beforeEach(() => {
  getPending.mockReset();
  apply.mockReset();
  discard.mockReset();
});

describe("WriteApprovalCard", () => {
  it("shows the real diff to approve, and only enables writing once it has loaded", async () => {
    let resolve!: (p: ProposedWrite) => void;
    getPending.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<WriteApprovalCard proposal={proposal()} />);

    const write = screen.getByRole("button", { name: /write to disk/i }) as HTMLButtonElement;
    expect(write.disabled).toBe(true);
    await act(async () => resolve(proposal({ diff: REAL })));

    expect(getPending).toHaveBeenCalledWith("pwr_abc");
    expect(screen.getByText(/ghp_realcontext/)).toBeTruthy();
    expect(screen.queryByText(/\*\*\*redacted\*\*\*/)).toBeNull();
    expect(write.disabled).toBe(false);
  });

  it("stamps UNVALIDATED with the reason and lists every check", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL }));
    render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/ghp_realcontext/);

    expect(screen.getByText("UNVALIDATED")).toBeTruthy();
    expect(screen.getAllByText(/kubeconform not installed/).length).toBeGreaterThan(0);
    for (const name of ["yaml_parse", "policy", "kubeconform"]) {
      expect(screen.getByText(name)).toBeTruthy();
    }
  });

  it("does not stamp a fully validated edit", async () => {
    const validated = proposal({
      validation: { ok: true, validated: true, unvalidated_reason: null,
                    checks: [{ name: "kubeconform", status: "pass", detail: "" }] },
    });
    getPending.mockResolvedValue({ ...validated, diff: REAL });
    render(<WriteApprovalCard proposal={validated} />);
    await screen.findByText(/ghp_realcontext/);
    expect(screen.queryByText("UNVALIDATED")).toBeNull();
    expect(screen.getByText(/validated/i)).toBeTruthy();
  });

  it("marks added and removed diff lines", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL }));
    const { container } = render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/ghp_realcontext/);
    expect(container.querySelector('[data-diff="add"]')?.textContent).toContain("replicas: 4");
    expect(container.querySelector('[data-diff="del"]')?.textContent).toContain("replicas: 2");
  });

  it("writes on approval and shows the result", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL }));
    apply.mockResolvedValue({ written: true, path: "prod/api.yaml", created: false });
    render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/ghp_realcontext/);

    fireEvent.click(screen.getByRole("button", { name: /write to disk/i }));
    await screen.findByText(/written to prod\/api\.yaml/i);
    expect(apply).toHaveBeenCalledWith("pwr_abc");
    expect(screen.queryByRole("button", { name: /write to disk/i })).toBeNull();
  });

  it("explains a conflict when the file changed on disk", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL }));
    apply.mockRejectedValue(new ApiError(
      "the file changed on disk since this edit was proposed; nothing was written", 409));
    render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/ghp_realcontext/);

    fireEvent.click(screen.getByRole("button", { name: /write to disk/i }));
    await screen.findByText(/changed on disk/i);
    expect(screen.queryByRole("button", { name: /write to disk/i })).toBeNull();
  });

  it("shows an expired proposal without a write button", async () => {
    getPending.mockRejectedValue(new ApiError("proposed edit expired or already handled", 404));
    render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/expired/i);
    expect(screen.queryByRole("button", { name: /write to disk/i })).toBeNull();
  });

  it("discards", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL }));
    discard.mockResolvedValue({ discarded: true });
    render(<WriteApprovalCard proposal={proposal()} />);
    await screen.findByText(/ghp_realcontext/);

    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    await screen.findByText(/discarded/i);
    expect(discard).toHaveBeenCalledWith("pwr_abc");
    expect(apply).not.toHaveBeenCalled();
  });

  it("labels a new file and shows the agent's reason", async () => {
    getPending.mockResolvedValue(proposal({ diff: REAL, created: true }));
    render(<WriteApprovalCard proposal={proposal({ created: true })} />);
    await waitFor(() => expect(screen.getByText(/new file/i)).toBeTruthy());
    expect(screen.getByText(/raise replicas to absorb load/)).toBeTruthy();
  });
});
