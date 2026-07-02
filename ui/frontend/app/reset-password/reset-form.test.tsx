import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ResetPasswordForm from "./reset-form";
import { ApiError, resetPassword } from "../../lib/api";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, resetPassword: vi.fn() };
});

function fillPasswords(value: string, confirm = value) {
  fireEvent.change(screen.getByLabelText("New password"), { target: { value } });
  fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: confirm } });
}

describe("ResetPasswordForm", () => {
  beforeEach(() => {
    vi.mocked(resetPassword).mockReset();
  });

  it("renders the missing-token branch and never shows the password form", () => {
    render(<ResetPasswordForm token="" />);

    expect(screen.getByText(/missing its token/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request a new link" })).toBeInTheDocument();
    expect(screen.queryByLabelText("New password")).toBeNull();
  });

  it("blocks mismatched passwords client-side without calling the API", () => {
    render(<ResetPasswordForm token="tok-123" />);

    fillPasswords("brand-new-password-9", "different-password-9");
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    expect(screen.getByText(/passwords do not match/i)).toBeInTheDocument();
    expect(resetPassword).not.toHaveBeenCalled();
  });

  it("shows the backend error for an invalid/expired token (HTTP 400)", async () => {
    vi.mocked(resetPassword).mockRejectedValueOnce(
      new ApiError("reset link is invalid or has expired", 400),
    );
    render(<ResetPasswordForm token="tok-123" />);

    fillPasswords("brand-new-password-9");
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText(/invalid or has expired/i)).toBeInTheDocument();
    expect(resetPassword).toHaveBeenCalledWith("tok-123", "brand-new-password-9");
  });

  it("surfaces a rate-limit message on HTTP 429", async () => {
    vi.mocked(resetPassword).mockRejectedValueOnce(new ApiError("Too many requests", 429));
    render(<ResetPasswordForm token="tok-123" />);

    fillPasswords("brand-new-password-9");
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument();
  });

  it("confirms all sessions are signed out on success", async () => {
    vi.mocked(resetPassword).mockResolvedValueOnce({ ok: true });
    render(<ResetPasswordForm token="tok-123" />);

    fillPasswords("brand-new-password-9");
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText(/all sessions have been signed out/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });
});
