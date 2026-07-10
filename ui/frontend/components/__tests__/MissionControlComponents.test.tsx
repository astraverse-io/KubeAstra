import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AstraGlyph } from "../AstraGlyph";
import { ReasoningStream } from "../ReasoningStream";
import { ToolCard, type ToolCardStep } from "../ToolCard";
import { MissionControlDiagnosis, InlineToken } from "../MissionControlDiagnosis";
import { CommandBar } from "../CommandBar";
import { HoldToAuthorize } from "../HoldToAuthorize";

describe("AstraGlyph", () => {
  it("renders an accessible SVG with title", () => {
    render(<AstraGlyph title="test glyph" />);
    expect(screen.getByRole("img", { name: "test glyph" })).toBeInTheDocument();
  });

  it("applies glow animation when animate is true", () => {
    const { container } = render(<AstraGlyph animate />);
    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("style")).toMatch(/mcGlow/);
  });
});

describe("ReasoningStream", () => {
  it("returns null when no tokens and not active", () => {
    const { container } = render(<ReasoningStream tokens={[]} active={false} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders tokens and marks region live", () => {
    render(<ReasoningStream tokens={["step one", "step two"]} active={false} />);
    expect(screen.getByText("step one")).toBeInTheDocument();
    expect(screen.getByText("step two")).toBeInTheDocument();
    expect(screen.getByText("Astra · reasoning trace")).toBeInTheDocument();
  });
});

describe("ToolCard", () => {
  const done: ToolCardStep = {
    tool: "kubectl",
    cmd: "kubectl get pods",
    summary: "found 3 pods",
    duration: "0.41s",
    status: "done",
    output: ["pod-1  Running", "pod-2  Running", "pod-3  Pending"],
  };

  it("renders summary and duration for done step", () => {
    render(<ToolCard step={done} idx={0} />);
    expect(screen.getByText("found 3 pods")).toBeInTheDocument();
    expect(screen.getByText("0.41s")).toBeInTheDocument();
  });

  it("shows queued label for pending step and disables toggle", () => {
    render(<ToolCard step={{ ...done, status: "pending" }} idx={1} />);
    expect(screen.getByText("queued")).toBeInTheDocument();
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
  });

  it("shows executing indicator for running step", () => {
    render(<ToolCard step={{ ...done, status: "running" }} idx={0} />);
    expect(screen.getByText(/executing/)).toBeInTheDocument();
  });

  it("calls onToggle when done step header is clicked", () => {
    const onToggle = vi.fn();
    render(<ToolCard step={done} idx={0} onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("renders expanded output when expanded=true", () => {
    render(<ToolCard step={done} idx={0} expanded />);
    expect(screen.getByText("kubectl get pods")).toBeInTheDocument();
    // testing-library normalizes whitespace; use a regex so multiple spaces match.
    expect(screen.getByText(/pod-1\s+Running/)).toBeInTheDocument();
  });
});

describe("MissionControlDiagnosis", () => {
  it("renders title, summary, and confidence", () => {
    render(
      <MissionControlDiagnosis
        severity="sev-1"
        title="OOMKilled loop"
        summary="memory limit too low"
        confidence={0.87}
      />
    );
    expect(screen.getByText("OOMKilled loop")).toBeInTheDocument();
    expect(screen.getByText("memory limit too low")).toBeInTheDocument();
    expect(screen.getByText("0.87")).toBeInTheDocument();
    expect(screen.getByText("SEV-1")).toBeInTheDocument();
  });

  it("renders authorize CTA when onAuthorize provided", () => {
    const onAuthorize = vi.fn();
    render(
      <MissionControlDiagnosis
        severity="sev-2"
        title="t"
        summary="s"
        onAuthorize={onAuthorize}
      />
    );
    fireEvent.click(screen.getByText(/REVIEW & AUTHORIZE FIX/i));
    expect(onAuthorize).toHaveBeenCalledOnce();
  });

  it("omits CTA when onAuthorize is undefined", () => {
    render(<MissionControlDiagnosis severity="info" title="t" summary="s" />);
    expect(screen.queryByText(/REVIEW & AUTHORIZE FIX/i)).not.toBeInTheDocument();
  });
});

describe("InlineToken", () => {
  it("wraps children in styled span", () => {
    render(<InlineToken variant="red">128Mi</InlineToken>);
    expect(screen.getByText("128Mi")).toBeInTheDocument();
  });
});

describe("CommandBar", () => {
  it("calls onSend with trimmed input on submit", () => {
    const onSend = vi.fn();
    render(<CommandBar onSend={onSend} />);
    const input = screen.getByRole("textbox", { name: /ask kubeastra/i });
    fireEvent.change(input, { target: { value: "  why is X broken " } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    expect(onSend).toHaveBeenCalledWith("why is X broken");
  });

  it("disables dispatch when input is empty", () => {
    render(<CommandBar onSend={vi.fn()} />);
    const send = screen.getByRole("button", { name: /dispatch/i });
    expect(send).toBeDisabled();
  });

  it("blocks submission while busy", () => {
    const onSend = vi.fn();
    render(<CommandBar onSend={onSend} busy />);
    // Input is disabled while busy — DOM change events skip the onChange handler.
    // Confirm both signals: input is disabled AND dispatch button is disabled.
    const input = screen.getByPlaceholderText(/investigating/i);
    expect(input).toBeDisabled();
    const send = screen.getByRole("button", { name: /dispatch/i });
    expect(send).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("renders quick probe chips and invokes onSend when clicked", () => {
    const onSend = vi.fn();
    render(
      <CommandBar
        onSend={onSend}
        quickProbes={[{ icon: "?", text: "list crashloop pods" }]}
      />
    );
    fireEvent.click(screen.getByText("list crashloop pods"));
    expect(onSend).toHaveBeenCalledWith("list crashloop pods");
  });
});

describe("HoldToAuthorize", () => {
  it("confirms immediately when Enter is pressed on the button", () => {
    vi.useFakeTimers();
    const onConfirm = vi.fn();
    render(<HoldToAuthorize onConfirm={onConfirm} holdMs={800} />);
    const button = screen.getByRole("button", { name: /hold space or press enter/i });
    button.focus();
    fireEvent.keyDown(button, { key: "Enter" });
    vi.advanceTimersByTime(500);
    expect(onConfirm).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("renders keyboard hint by default", () => {
    render(<HoldToAuthorize onConfirm={vi.fn()} />);
    expect(screen.getByText(/Keyboard: hold/i)).toBeInTheDocument();
  });

  it("does not render keyboard hint when keyboardFallback is false", () => {
    render(<HoldToAuthorize onConfirm={vi.fn()} keyboardFallback={false} />);
    expect(screen.queryByText(/Keyboard: hold/i)).not.toBeInTheDocument();
  });
});
