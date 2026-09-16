import * as vscode from "vscode";

const COOKIE_KEY = "kubeastra.cookie";

/**
 * Server-mode authentication for the extension.
 *
 * KubeAstra server mode authenticates browsers with an HttpOnly session cookie
 * (`k8s_devops_auth` by default), minted by `POST /api/auth/login`. There is no
 * OAuth, no bearer user-login, and no hosted backend — see the plan. The
 * extension host logs in on the user's behalf, keeps the raw cookie in
 * SecretStorage, and replays it on every proxied request (`apiProxy.ts`).
 *
 * When `AUTH_ENABLED=false` on the backend, `/api/auth/me` answers without a
 * cookie and no sign-in is needed; we detect that and skip the prompt.
 */
export class AuthManager {
  private readonly _onChange = new vscode.EventEmitter<void>();
  readonly onChange = this._onChange.event;

  constructor(private readonly ctx: vscode.ExtensionContext) {}

  /** Configured backend base URL, trailing slash stripped, or null if unset. */
  backendUrl(): string | null {
    const raw = vscode.workspace
      .getConfiguration("kubeastra")
      .get<string>("backendUrl")
      ?.trim();
    return raw ? raw.replace(/\/$/, "") : null;
  }

  /** The stored `name=value` cookie string, or null. */
  async getCookie(): Promise<string | null> {
    return (await this.ctx.secrets.get(COOKIE_KEY)) ?? null;
  }

  /**
   * Whether the backend requires auth. Probes `/api/auth/me`: 200 with a stored
   * cookie (or with auth disabled) means authenticated; 401 means sign-in
   * needed. Returns false (no auth required) when the probe succeeds without a
   * cookie.
   */
  async isAuthRequired(base: string): Promise<boolean> {
    const cookie = await this.getCookie();
    try {
      const res = await fetch(`${base}/api/auth/me`, {
        headers: cookie ? { cookie } : {},
      });
      return res.status === 401;
    } catch {
      // Unreachable backend: treat as auth-required so the user is prompted.
      return true;
    }
  }

  async isSignedIn(): Promise<boolean> {
    const base = this.backendUrl();
    if (!base) return false;
    return !(await this.isAuthRequired(base));
  }

  /**
   * Prompt for a backend URL (if unset) and credentials, then log in. Captures
   * the `Set-Cookie` from `/api/auth/login` and persists it. No-op with a
   * friendly message when the backend has auth disabled.
   */
  async signIn(): Promise<void> {
    let base = this.backendUrl();
    if (!base) {
      const entered = await vscode.window.showInputBox({
        title: "KubeAstra backend URL",
        prompt: "Base URL of your self-hosted KubeAstra backend",
        placeHolder: "http://localhost:8000",
        ignoreFocusOut: true,
        validateInput: (v) =>
          /^https?:\/\/.+/.test(v.trim()) ? null : "Must start with http:// or https://",
      });
      if (!entered) return;
      base = entered.trim().replace(/\/$/, "");
      await vscode.workspace
        .getConfiguration("kubeastra")
        .update("backendUrl", base, vscode.ConfigurationTarget.Global);
    }

    if (!(await this.isAuthRequired(base))) {
      vscode.window.showInformationMessage(
        "KubeAstra: this backend has authentication disabled — no sign-in needed.",
      );
      this._onChange.fire();
      return;
    }

    const username = await vscode.window.showInputBox({
      title: "KubeAstra sign in",
      prompt: "Username",
      ignoreFocusOut: true,
    });
    if (!username) return;
    const password = await vscode.window.showInputBox({
      title: "KubeAstra sign in",
      prompt: "Password",
      password: true,
      ignoreFocusOut: true,
    });
    if (password === undefined) return;

    let res: Response;
    try {
      res = await fetch(`${base}/api/auth/login`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
    } catch (err) {
      vscode.window.showErrorMessage(
        `KubeAstra: backend unreachable (${(err as Error).message})`,
      );
      return;
    }

    if (!res.ok) {
      vscode.window.showErrorMessage(
        res.status === 401
          ? "KubeAstra sign-in failed: invalid username or password"
          : `KubeAstra sign-in failed (HTTP ${res.status})`,
      );
      return;
    }

    const cookie = extractCookie(res);
    if (!cookie) {
      vscode.window.showErrorMessage(
        "KubeAstra sign-in succeeded but no session cookie was returned.",
      );
      return;
    }
    await this.ctx.secrets.store(COOKIE_KEY, cookie);
    this._onChange.fire();
    vscode.window.showInformationMessage("KubeAstra: signed in");
  }

  async signOut(): Promise<void> {
    const base = this.backendUrl();
    const cookie = await this.getCookie();
    if (base && cookie) {
      try {
        await fetch(`${base}/api/auth/logout`, {
          method: "POST",
          headers: { cookie },
        });
      } catch {
        // Best-effort; clearing the local secret is what matters.
      }
    }
    await this.ctx.secrets.delete(COOKIE_KEY);
    this._onChange.fire();
    vscode.window.showInformationMessage("KubeAstra: signed out");
  }
}

/**
 * Pull the session cookie out of a login response as a `name=value` string.
 * Uses `getSetCookie()` (undici, Node 18+) which preserves multiple cookies;
 * we keep only the cookie pair, dropping attributes (Path, HttpOnly, …).
 */
function extractCookie(res: Response): string | null {
  const headers = res.headers as Headers & { getSetCookie?(): string[] };
  const all = headers.getSetCookie?.() ?? [];
  const raw = all[0] ?? res.headers.get("set-cookie");
  if (!raw) return null;
  const pair = raw.split(";", 1)[0]?.trim();
  return pair || null;
}
