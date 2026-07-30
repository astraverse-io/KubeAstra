import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DesktopSettings from "../DesktopSettings";
import {
  fetchDesktopSettings,
  fetchDesktopSetup,
  forgetDesktopSecret,
  setupDesktopEmbeddings,
  testAlertmanager,
  updateDesktopSettings,
  type DesktopSettingsState,
  type DesktopSetupState,
} from "../../lib/api";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    fetchDesktopSettings: vi.fn(),
    fetchDesktopSetup: vi.fn(),
    forgetDesktopSecret: vi.fn(),
    setupDesktopEmbeddings: vi.fn(),
    testAlertmanager: vi.fn(),
    updateDesktopSettings: vi.fn(),
  };
});

const SETTINGS: DesktopSettingsState = {
  memory_enabled: true,
  remote_diagnostics_enabled: false,
  memory_mode: "keyword",
  memory_available: false,
  keychain_secure: true,
  keychain_backend: "macOS",
  alertmanager_url: "http://localhost:9093",
  notifications_enabled: true,
  default_cluster_context: "kind-kubeastra-dev",
  kubectl_path: "/Applications/Docker.app/Contents/Resources/bin/kubectl",
  missing_auth_plugins: [],
};

const SETUP: DesktopSetupState = {
  configured: true,
  llm_provider: "gemini",
  needs_embeddings_key: false,
  memory_available: false,
  memory_mode: "keyword",
  keychain_secure: true,
  keychain_backend: "macOS",
};

function mount(settings = SETTINGS, setup = SETUP) {
  vi.mocked(fetchDesktopSettings).mockResolvedValue(settings);
  vi.mocked(fetchDesktopSetup).mockResolvedValue(setup);
  const onClose = vi.fn();
  const onCredentialCleared = vi.fn();
  render(<DesktopSettings onClose={onClose} onCredentialCleared={onCredentialCleared} />);
  return { onClose, onCredentialCleared };
}

describe("DesktopSettings", () => {
  beforeEach(() => {
    vi.mocked(fetchDesktopSettings).mockReset();
    vi.mocked(fetchDesktopSetup).mockReset();
    vi.mocked(forgetDesktopSecret).mockReset();
    vi.mocked(testAlertmanager).mockReset();
    vi.mocked(updateDesktopSettings).mockReset();
  });

  it("shows the cluster background investigations target and the kubectl in use", async () => {
    mount();

    expect(await screen.findByText("kind-kubeastra-dev")).toBeInTheDocument();
    expect(
      screen.getByText("/Applications/Docker.app/Contents/Resources/bin/kubectl"),
    ).toBeInTheDocument();
  });

  it("says background investigations will refuse to run when no cluster is chosen", async () => {
    mount({ ...SETTINGS, default_cluster_context: "" });

    expect(await screen.findByText("No cluster chosen")).toBeInTheDocument();
    expect(screen.getByText(/refuse to run rather than fall back/i)).toBeInTheDocument();
  });

  it("names credential plugins it could not find", async () => {
    mount({ ...SETTINGS, missing_auth_plugins: ["gke-gcloud-auth-plugin", "kubelogin"] });

    expect(
      await screen.findByText(/gke-gcloud-auth-plugin, kubelogin/),
    ).toBeInTheDocument();
  });

  // The reason this screen exists: a rotated key used to brick the app,
  // because `configured` is sticky and nothing could clear a stored key.
  it("forgets the stored key and tells the parent, so setup can be re-run", async () => {
    vi.mocked(forgetDesktopSecret).mockResolvedValue({ ok: true });
    const { onCredentialCleared } = mount();

    fireEvent.click(await screen.findByRole("button", { name: "Forget this key" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, forget it" }));

    await waitFor(() => expect(forgetDesktopSecret).toHaveBeenCalledWith("llm.gemini"));
    await waitFor(() => expect(onCredentialCleared).toHaveBeenCalled());
  });

  it("asks for confirmation before forgetting a key", async () => {
    mount();

    fireEvent.click(await screen.findByRole("button", { name: "Forget this key" }));

    expect(screen.getByRole("button", { name: "Yes, forget it" })).toBeInTheDocument();
    expect(forgetDesktopSecret).not.toHaveBeenCalled();
  });

  it("warns when the keychain is not secure, not only during first run", async () => {
    mount({ ...SETTINGS, keychain_secure: false, keychain_backend: "plaintext" });

    expect(await screen.findByText(/No system keychain is available/i)).toBeInTheDocument();
    expect(screen.getByText(/plaintext/)).toBeInTheDocument();
  });

  // Polling happens on a background thread, so an unreachable URL would
  // otherwise fail where nobody can see it.
  it("checks the Alertmanager URL before storing it", async () => {
    vi.mocked(testAlertmanager).mockResolvedValue({
      ok: true,
      url: "http://am:9093",
      firing: 2,
    });
    vi.mocked(updateDesktopSettings).mockResolvedValue(SETTINGS);
    mount();

    fireEvent.change(await screen.findByLabelText("Alertmanager URL"), {
      target: { value: "http://am:9093" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Test & save" }));

    await waitFor(() => expect(testAlertmanager).toHaveBeenCalledWith("http://am:9093"));
    await waitFor(() =>
      expect(updateDesktopSettings).toHaveBeenCalledWith({ alertmanager_url: "http://am:9093" }),
    );
    expect(await screen.findByText(/2 alerts firing/i)).toBeInTheDocument();
  });

  it("does not store an Alertmanager URL it could not reach", async () => {
    vi.mocked(testAlertmanager).mockRejectedValue(new Error("Connection refused"));
    mount();

    fireEvent.change(await screen.findByLabelText("Alertmanager URL"), {
      target: { value: "http://nope:9093" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Test & save" }));

    await waitFor(() => expect(screen.getByText("Connection refused")).toBeInTheDocument());
    expect(updateDesktopSettings).not.toHaveBeenCalled();
  });

  it("cannot enable notifications with nowhere to poll", async () => {
    mount({ ...SETTINGS, alertmanager_url: "", notifications_enabled: false });

    const toggle = await screen.findByLabelText("Enable notifications");
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/Set an Alertmanager URL first/i)).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const { onClose } = mount();
    await screen.findByText("kind-kubeastra-dev");

    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).toHaveBeenCalled();
  });
});

describe("DesktopSettings — embeddings key", () => {
  beforeEach(() => {
    vi.mocked(fetchDesktopSettings).mockReset();
    vi.mocked(fetchDesktopSetup).mockReset();
    vi.mocked(setupDesktopEmbeddings).mockReset();
  });

  it("verifies an embeddings key against the provider before storing it", async () => {
    vi.mocked(setupDesktopEmbeddings).mockResolvedValue({
      ok: true,
      provider: "voyage",
      dim: 1024,
    });
    mount();

    fireEvent.change(await screen.findByLabelText("Embeddings API key"), {
      target: { value: "pa-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    await waitFor(() =>
      expect(setupDesktopEmbeddings).toHaveBeenCalledWith({
        provider: "voyage",
        api_key: "pa-secret",
      }),
    );
    expect(await screen.findByText(/1024 dimensions/)).toBeInTheDocument();
  });

  it("surfaces a rejected embeddings key instead of silently staying keyword-only", async () => {
    vi.mocked(setupDesktopEmbeddings).mockRejectedValue(new Error("invalid api key"));
    mount();

    fireEvent.change(await screen.findByLabelText("Embeddings API key"), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    expect(await screen.findByText("invalid api key")).toBeInTheDocument();
  });

  it("will not submit an empty embeddings key", async () => {
    mount();
    expect(await screen.findByRole("button", { name: "Save key" })).toBeDisabled();
  });
});
