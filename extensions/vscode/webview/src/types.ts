/**
 * Host→webview message shapes. Kept in sync with `src/protocol.ts` on the host
 * side (the webview is a separate Vite project with its own tsconfig, so the
 * union is mirrored here rather than imported across the project boundary).
 */
export type HostToWebview =
  | { type: "api-response"; id: string; status: number; body: unknown }
  | { type: "api-chunk"; id: string; data: string }
  | { type: "api-done"; id: string; status: number }
  | { type: "api-error"; id: string; message: string }
  | { type: "auth-state"; signedIn: boolean; backendUrl: string | null; authRequired: boolean }
  | { type: "prompt"; text: string };
