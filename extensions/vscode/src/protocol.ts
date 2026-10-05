/**
 * Message protocol between the extension host and the webview.
 *
 * The webview never touches the network directly: desktop-mode's guard rejects
 * a `vscode-webview://` Origin outright, and carrying the server-mode HttpOnly
 * cookie from inside a webview is fragile. So every backend call is a message
 * to the host, which owns the credential and makes the real fetch. See the
 * plan (`internal_docs/features/FEATURE_ROADMAP.md` §2, corrected) for why.
 */

/** Sent webview → host to perform a one-shot JSON API call. */
export interface ApiRequest {
  type: "api";
  id: string;
  path: string; // e.g. "/api/chat"
  method?: string; // default GET
  body?: unknown;
  stream?: boolean; // true → host replies with api-chunk*/api-done instead of api-response
}

/** Sent webview → host to abort an in-flight streaming request. */
export interface ApiAbort {
  type: "api-abort";
  id: string;
}

/** Sent host → webview: full response to a non-streaming ApiRequest. */
export interface ApiResponse {
  type: "api-response";
  id: string;
  status: number;
  body: unknown;
}

/** Sent host → webview: one raw chunk of a streaming response body. */
export interface ApiChunk {
  type: "api-chunk";
  id: string;
  data: string;
}

/** Sent host → webview: a streaming response has ended. */
export interface ApiDone {
  type: "api-done";
  id: string;
  status: number;
}

/** Sent host → webview: a request failed before/independent of an HTTP status. */
export interface ApiError {
  type: "api-error";
  id: string;
  message: string;
}

/** Sent host → webview: auth/connection state changed; the UI should refresh. */
export interface AuthState {
  type: "auth-state";
  signedIn: boolean;
  backendUrl: string | null;
  authRequired: boolean;
}

/** Current Kubernetes cluster reachability, derived from /api/cluster/autodetect. */
export interface ClusterInfo {
  connected: boolean;
  name?: string;
  context?: string;
}

/** Sent host → webview: the backend's cluster status changed. */
export interface ClusterState extends ClusterInfo {
  type: "cluster-state";
}

/** Sent host → webview: seed the command bar with text (investigate file/selection). */
export interface Prompt {
  type: "prompt";
  text: string;
}

export type HostToWebview =
  | ApiResponse
  | ApiChunk
  | ApiDone
  | ApiError
  | AuthState
  | ClusterState
  | Prompt;

export type WebviewToHost = ApiRequest | ApiAbort | { type: "ready" };
