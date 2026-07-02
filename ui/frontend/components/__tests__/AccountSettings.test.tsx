import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AccountSettings from "../AccountSettings";
import { ApiError, changePassword, updateEmail, type AuthUser } from "../../lib/api";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, changePassword: vi.fn(), updateEmail: vi.fn() };
});

const USER: AuthUser = {
  id: "u1",
  username: "alice",
  display_name: "Alice",
  email: null,
  role: "user",
};

function renderPanel() {
  const onClose = vi.fn();
  const onAuthChanged = vi.fn();
  render(<AccountSettings user={USER} onClose={onClose} onAuthChanged={onAuthChanged} />);
  return { onClose, onAuthChanged };
}

/** Scope queries to the section whose <h3> matches `heading`. */
function section(heading: string) {
  const el = screen.getByRole("heading", { name: heading }).closest("section");
  if (!el) throw new Error(`section not found for heading: ${heading}`);
  return within(el as HTMLElement);
}

describe("AccountSettings — update email", () => {
  beforeEach(() => {
    vi.mocked(updateEmail).mockReset();
    vi.mocked(changePassword).mockReset();
  });

  it("sends the email with the current password and reports success", async () => {
    vi.mocked(updateEmail).mockResolvedValueOnce({
      auth_enabled: true,
      allow_signup: true,
      user: { ...USER, email: "alice@example.com" },
    });
    const { onAuthChanged } = renderPanel();
    const email = section("Email");

    fireEvent.change(email.getByLabelText("Email address"), { target: { value: "alice@example.com" } });
    fireEvent.change(email.getByLabelText("Current password"), { target: { value: "correct horse" } });
    fireEvent.click(email.getByRole("button", { name: "Save email" }));

    expect(await email.findByText(/email updated/i)).toBeInTheDocument();
    expect(updateEmail).toHaveBeenCalledWith("alice@example.com", "correct horse");
    expect(onAuthChanged).toHaveBeenCalledTimes(1);
  });

  it("requires the current password before it will submit", () => {
    renderPanel();
    const email = section("Email");

    fireEvent.change(email.getByLabelText("Email address"), { target: { value: "alice@example.com" } });
    // No current password entered -> button stays disabled, API never called.
    expect(email.getByRole("button", { name: "Save email" })).toBeDisabled();
    fireEvent.click(email.getByRole("button", { name: "Save email" }));
    expect(updateEmail).not.toHaveBeenCalled();
  });

  it("surfaces the backend rejection when the current password is wrong", async () => {
    vi.mocked(updateEmail).mockRejectedValueOnce(new ApiError("current password is incorrect", 400));
    renderPanel();
    const email = section("Email");

    fireEvent.change(email.getByLabelText("Email address"), { target: { value: "alice@example.com" } });
    fireEvent.change(email.getByLabelText("Current password"), { target: { value: "WRONG" } });
    fireEvent.click(email.getByRole("button", { name: "Save email" }));

    expect(await email.findByText(/current password is incorrect/i)).toBeInTheDocument();
  });
});

describe("AccountSettings — change password", () => {
  beforeEach(() => {
    vi.mocked(updateEmail).mockReset();
    vi.mocked(changePassword).mockReset();
  });

  it("blocks a mismatched confirmation without calling the API", () => {
    renderPanel();
    const pw = section("Change password");

    fireEvent.change(pw.getByLabelText("Current password"), { target: { value: "old-password-123" } });
    fireEvent.change(pw.getByLabelText("New password"), { target: { value: "brand-new-password-9" } });
    fireEvent.change(pw.getByLabelText("Confirm new password"), { target: { value: "different-9" } });
    fireEvent.click(pw.getByRole("button", { name: "Change password" }));

    expect(pw.getByText(/do not match/i)).toBeInTheDocument();
    expect(changePassword).not.toHaveBeenCalled();
  });

  it("submits a matching new password and reports the session sign-out", async () => {
    vi.mocked(changePassword).mockResolvedValueOnce({ ok: true });
    renderPanel();
    const pw = section("Change password");

    fireEvent.change(pw.getByLabelText("Current password"), { target: { value: "old-password-123" } });
    fireEvent.change(pw.getByLabelText("New password"), { target: { value: "brand-new-password-9" } });
    fireEvent.change(pw.getByLabelText("Confirm new password"), { target: { value: "brand-new-password-9" } });
    fireEvent.click(pw.getByRole("button", { name: "Change password" }));

    expect(await pw.findByText(/password changed/i)).toBeInTheDocument();
    expect(changePassword).toHaveBeenCalledWith("old-password-123", "brand-new-password-9");
  });
});
