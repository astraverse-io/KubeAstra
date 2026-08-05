import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SetupNotice from "./SetupNotice";

/**
 * The point of this component is that the advice is *correct for your
 * install*, not merely present. "Add GEMINI_API_KEY" is wrong advice for an
 * Ollama user and "run ollama serve" is wrong for everyone else, so the tests
 * below check that each variant does not leak the other's instructions.
 */

describe("SetupNotice", () => {
  it("tells a Gemini install where to get a key", () => {
    render(<SetupNotice provider="gemini" />);

    expect(screen.getByText(/No language model connected/i)).toBeTruthy();
    expect(screen.getByText("GEMINI_API_KEY")).toBeTruthy();
    expect(screen.getByRole("link", { name: /aistudio\.google\.com/i })).toBeTruthy();
  });

  it("tells an Ollama install to start the server, and never mentions an API key", () => {
    render(<SetupNotice provider="ollama" />);

    expect(screen.getByText("ollama serve")).toBeTruthy();
    expect(screen.getByText("OLLAMA_BASE_URL")).toBeTruthy();
    // The failure this guards against: sending someone running a local model
    // off to buy a cloud API key.
    expect(screen.queryByText("GEMINI_API_KEY")).toBeNull();
  });

  it("does not send a Gemini install to install Ollama", () => {
    render(<SetupNotice provider="gemini" />);
    expect(screen.queryByText("ollama serve")).toBeNull();
  });

  it("falls back to the key instructions when the provider is unknown", () => {
    render(<SetupNotice provider={undefined} />);
    expect(screen.getByText("GEMINI_API_KEY")).toBeTruthy();
  });

  it("is a status region, not an alert — nothing is broken", () => {
    const { container } = render(<SetupNotice provider="gemini" />);
    // `alert` interrupts a screen reader mid-sentence. This is a setup hint on
    // an idle screen; `status` announces politely when the user gets there.
    expect(container.querySelector('[role="status"]')).toBeTruthy();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("says the cluster tools still work, so nobody thinks the app is dead", () => {
    render(<SetupNotice provider="gemini" />);
    expect(screen.getByText(/Cluster tools still work/i)).toBeTruthy();
  });
});
