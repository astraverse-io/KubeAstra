import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import GitOpsPreviewModal from "../GitOpsPreviewModal";

const preview = {
  preview_token: "gpv_x",
  diff: "--- a/base/api.yaml\n+++ b/base/api.yaml\n@@\n-  replicas: 3\n+  replicas: 5\n",
  files: { "base/api.yaml": "spec:\n  replicas: 5\n" },
  branch: "kubeastra/fix-api-replicas-inv_abc",
  title: "fix(api-gateway): low replicas",
};

describe("GitOpsPreviewModal", () => {
  it("shows the real diff before opening anything", () => {
    render(<GitOpsPreviewModal preview={preview} onConfirm={() => {}} onCancel={() => {}} />);
    expect(screen.getByText(/replicas: 5/)).toBeTruthy();
    expect(screen.getByText(/kubeastra\/fix-api-replicas/)).toBeTruthy();
  });

  it("calls onConfirm with the token when the user opens the PR", () => {
    const onConfirm = vi.fn();
    render(<GitOpsPreviewModal preview={preview} onConfirm={onConfirm} onCancel={() => {}} />);
    fireEvent.click(screen.getByText(/Open pull request/i));
    expect(onConfirm).toHaveBeenCalledWith("gpv_x");
  });
});
