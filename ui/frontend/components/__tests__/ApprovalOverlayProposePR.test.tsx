import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MissionControlApprovalOverlay } from "../MissionControlApprovalOverlay";

/**
 * The PR path is additive: the overlay shows it only when the parent supplies
 * onProposePR (i.e. a repo is connected and the action is file-expressible).
 * Every existing call site omits the prop and must be unchanged.
 */
describe("MissionControlApprovalOverlay — propose PR CTA", () => {
  const base = {
    title: "Scale api-gateway to 5",
    onClose: () => {},
    onConfirm: () => {},
  };

  it("hides the PR button when no handler is supplied", () => {
    render(<MissionControlApprovalOverlay {...base} />);
    expect(screen.queryByText(/PROPOSE VIA PULL REQUEST/i)).toBeNull();
  });

  it("shows the PR button and fires the handler when supplied", () => {
    const onProposePR = vi.fn();
    render(<MissionControlApprovalOverlay {...base} onProposePR={onProposePR} />);
    fireEvent.click(screen.getByText(/PROPOSE VIA PULL REQUEST/i));
    expect(onProposePR).toHaveBeenCalledTimes(1);
  });
});
