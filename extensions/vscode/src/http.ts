/**
 * Backend fetch with the session cookie attached. The single place that joins
 * the configured base URL to a path and adds the cookie header, shared by the
 * auth probe, the cluster poller, and the API proxy so the "authed GET" shape
 * lives in one spot.
 */
export async function authedFetch(
  base: string,
  path: string,
  cookie: string | null,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (cookie) headers.set("cookie", cookie);
  return fetch(`${base}${path}`, { ...init, headers });
}
