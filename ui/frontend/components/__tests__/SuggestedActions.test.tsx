import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SuggestedActions, firstExecutableAction, type SuggestedAction } from "../SuggestedActions";

const manualAction: SuggestedAction = {
  type: "trace",
  action_kind: "manual",
  label: "Trace source/config",
  follow_up_prompt: "Trace where the durable fix lives. Resource: jenkins-legacy-0",
};

const writeAction: SuggestedAction = {
  type: "apply",
  action_kind: "write_command",
  label: "Restart deployment",
  command: "kubectl rollout restart deployment/app -n ci",
  confirm: true,
};

describe("SuggestedActions", () => {
  it("renders a manual action as a 'Trace source/config' follow-up button", () => {
    render(<SuggestedActions actions={[manualAction]} onExecute={vi.fn()} onFollowUp={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Trace source/config" })).toBeInTheDocument();
    // Manual actions never render the command as a <code> block.
    expect(document.querySelector("code")).toBeNull();
  });

  it("submits the follow_up_prompt and never calls onExecute for a manual action", () => {
    const onExecute = vi.fn();
    const onFollowUp = vi.fn();
    render(<SuggestedActions actions={[manualAction]} onExecute={onExecute} onFollowUp={onFollowUp} />);

    fireEvent.click(screen.getByRole("button", { name: "Trace source/config" }));

    expect(onFollowUp).toHaveBeenCalledTimes(1);
    expect(onFollowUp).toHaveBeenCalledWith(manualAction.follow_up_prompt);
    expect(onExecute).not.toHaveBeenCalled();
  });

  it("renders an executable action with its command and routes the click to onExecute", () => {
    const onExecute = vi.fn();
    const onFollowUp = vi.fn();
    render(<SuggestedActions actions={[writeAction]} onExecute={onExecute} onFollowUp={onFollowUp} />);

    expect(screen.getByText(writeAction.command!)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review and execute fix" }));

    expect(onExecute).toHaveBeenCalledWith(writeAction);
    expect(onFollowUp).not.toHaveBeenCalled();
  });

  it("renders manual and executable actions together without key/render errors", () => {
    // A manual action has no `command`; it must not collide with key handling.
    render(<SuggestedActions actions={[writeAction, manualAction]} onExecute={vi.fn()} onFollowUp={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Review and execute fix" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Trace source/config" })).toBeInTheDocument();
  });
});

describe("firstExecutableAction", () => {
  it("returns undefined when the only action is a manual trace (no empty-command execution)", () => {
    expect(firstExecutableAction([manualAction])).toBeUndefined();
  });

  it("skips manual actions and returns the first executable one", () => {
    expect(firstExecutableAction([manualAction, writeAction])).toBe(writeAction);
  });

  it("ignores actions with an empty command", () => {
    const emptyCmd: SuggestedAction = { action_kind: "write_command", label: "x", command: "  " };
    expect(firstExecutableAction([emptyCmd, writeAction])).toBe(writeAction);
  });

  it("returns undefined for an empty or missing list", () => {
    expect(firstExecutableAction([])).toBeUndefined();
    expect(firstExecutableAction(undefined)).toBeUndefined();
  });
});
