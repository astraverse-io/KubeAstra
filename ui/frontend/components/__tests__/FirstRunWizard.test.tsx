import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import FirstRunWizard from "../FirstRunWizard";
import type { DesktopSetupState } from "../../lib/api";

vi.mock("../../lib/api", () => ({
  setupDesktopLlm: vi.fn(),
  setupDesktopEmbeddings: vi.fn(),
}));

import { setupDesktopLlm, setupDesktopEmbeddings } from "../../lib/api";

const baseState: DesktopSetupState = {
  configured: false,
  llm_provider: null,
  needs_embeddings_key: false,
  memory_available: false,
  memory_mode: "keyword",
  keychain_secure: true,
  keychain_backend: "Keyring",
};

function renderWizard(overrides: Partial<DesktopSetupState> = {}, onComplete = vi.fn()) {
  render(
    <FirstRunWizard state={{ ...baseState, ...overrides }} onComplete={onComplete} />,
  );
  return { onComplete };
}

function advanceToKeyStep() {
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FirstRunWizard", () => {
  it("offers the four supported providers", () => {
    renderWizard();
    expect(screen.getByRole("button", { name: /anthropic/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /openai/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /gemini/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ollama/i })).toBeInTheDocument();
  });

  it("requires a key before the test button becomes usable", async () => {
    renderWizard();
    advanceToKeyStep();

    const submit = screen.getByRole("button", { name: /test connection/i });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-ant-123" } });
    expect(submit).toBeEnabled();
  });

  it("masks the key input", async () => {
    renderWizard();
    advanceToKeyStep();
    expect(screen.getByLabelText(/api key/i)).toHaveAttribute("type", "password");
  });

  it("completes when the provider needs no embeddings key", async () => {
    vi.mocked(setupDesktopLlm).mockResolvedValue({
      ok: true,
      provider: "openai",
      needs_embeddings_key: false,
    });
    const { onComplete } = renderWizard();

    fireEvent.click(screen.getByRole("button", { name: /openai/i }));
    advanceToKeyStep();
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await waitFor(() => expect(onComplete).toHaveBeenCalled());
  });

  it("advances to the embeddings step for Anthropic", async () => {
    vi.mocked(setupDesktopLlm).mockResolvedValue({
      ok: true,
      provider: "anthropic",
      needs_embeddings_key: true,
    });
    const { onComplete } = renderWizard();

    advanceToKeyStep();
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-ant-test" } });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await waitFor(() =>
      expect(screen.getByText(/investigation memory/i)).toBeInTheDocument(),
    );
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("lets the user skip embeddings and keep keyword memory", async () => {
    vi.mocked(setupDesktopLlm).mockResolvedValue({
      ok: true,
      provider: "anthropic",
      needs_embeddings_key: true,
    });
    const { onComplete } = renderWizard();

    advanceToKeyStep();
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-ant-test" } });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    const skip = await screen.findByRole("button", { name: /skip/i });
    fireEvent.click(skip);
    expect(onComplete).toHaveBeenCalled();
  });

  it("shows the provider's own error and stays on the step", async () => {
    vi.mocked(setupDesktopLlm).mockRejectedValue(
      new Error("Could not verify anthropic: invalid x-api-key"),
    );
    const { onComplete } = renderWizard();

    advanceToKeyStep();
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-bad" } });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid x-api-key/i);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("skips key entry for Ollama", async () => {
    vi.mocked(setupDesktopLlm).mockResolvedValue({
      ok: true,
      provider: "ollama",
      needs_embeddings_key: false,
    });
    renderWizard();

    fireEvent.click(screen.getByRole("button", { name: /ollama/i }));
    advanceToKeyStep();

    expect(screen.queryByLabelText(/api key/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /test connection/i })).toBeEnabled();
  });

  it("warns when no system keychain is available", () => {
    renderWizard({ keychain_secure: false, keychain_backend: "fail.Keyring" });
    expect(screen.getByRole("status")).toHaveTextContent(/no system keychain/i);
  });

  it("does not warn when the keychain is healthy", () => {
    renderWizard({ keychain_secure: true });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("sends the selected embeddings provider", async () => {
    vi.mocked(setupDesktopLlm).mockResolvedValue({
      ok: true,
      provider: "anthropic",
      needs_embeddings_key: true,
    });
    vi.mocked(setupDesktopEmbeddings).mockResolvedValue({
      ok: true,
      provider: "voyage",
      dim: 1024,
    });
    renderWizard();

    advanceToKeyStep();
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-ant" } });
    fireEvent.click(screen.getByRole("button", { name: /test connection/i }));

    await screen.findByLabelText(/embeddings provider/i);
    fireEvent.change(screen.getByLabelText(/^api key$/i), { target: { value: "pa-voyage-key" } });
    fireEvent.click(screen.getByRole("button", { name: /enable memory/i }));

    await waitFor(() =>
      expect(setupDesktopEmbeddings).toHaveBeenCalledWith({
        provider: "voyage",
        api_key: "pa-voyage-key",
      }),
    );
  });
});
