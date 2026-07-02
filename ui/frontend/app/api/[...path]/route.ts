export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

function resolveApiBaseUrl(): string {
  return (process.env.API_BASE_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
}

async function proxyRequest(request: Request, context: RouteContext): Promise<Response> {
  const { path } = await context.params;
  const upstreamUrl = new URL(`${resolveApiBaseUrl()}/api/${path.join("/")}`);
  upstreamUrl.search = new URL(request.url).search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();

  const upstream = await fetch(upstreamUrl, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
    redirect: "manual",
  });

  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-length");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export async function GET(request: Request, context: RouteContext) {
  return proxyRequest(request, context);
}

export async function POST(request: Request, context: RouteContext) {
  return proxyRequest(request, context);
}

export async function PUT(request: Request, context: RouteContext) {
  return proxyRequest(request, context);
}

export async function PATCH(request: Request, context: RouteContext) {
  return proxyRequest(request, context);
}

export async function DELETE(request: Request, context: RouteContext) {
  return proxyRequest(request, context);
}
