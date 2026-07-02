import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ForgotPasswordPage from "./page";
import { ApiError, forgotPassword } from "../../lib/api";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, forgotPassword: vi.fn() };
});

const GENERIC = /if an account with that email exists/i;

describe("ForgotPasswordPage", () => {
  beforeEach(() => {
    vi.mocked(forgotPassword).mockReset();
  });

  it("shows the generic success message after submitting an email", async () => {
    vi.mocked(forgotPassword).mockResolvedValueOnce({ ok: true });
    render(<ForgotPasswordPage />);

    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "alice@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByText(GENERIC)).toBeInTheDocument();
    expect(forgotPassword).toHaveBeenCalledWith("alice@example.com");
    // The email input is gone once the generic confirmation is shown.
    expect(screen.queryByLabelText("Email address")).toBeNull();
  });

  it("shows the same generic confirmation regardless of whether the email exists", async () => {
    vi.mocked(forgotPassword).mockResolvedValue({ ok: true });
    render(<ForgotPasswordPage />);

    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "ghost@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    // No enumeration: the UI never reveals account existence.
    expect(await screen.findByText(GENERIC)).toBeInTheDocument();
  });

  it("surfaces a rate-limit message on HTTP 429", async () => {
    vi.mocked(forgotPassword).mockRejectedValueOnce(new ApiError("Too many requests", 429));
    render(<ForgotPasswordPage />);

    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "alice@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument();
    // Stayed on the form (no false success).
    expect(screen.queryByText(GENERIC)).toBeNull();
  });
});
