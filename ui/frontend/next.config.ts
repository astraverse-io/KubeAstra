import type { NextConfig } from "next";

// Desktop builds emit a static export that FastAPI serves from the same
// origin as the API, so the app/api proxy route (a Route Handler, which
// static export does not support) is excluded from the build instead.
const isDesktop = process.env.KUBEASTRA_BUILD_TARGET === "desktop";

const nextConfig: NextConfig = isDesktop
  ? {
      output: "export",
      // Emits /chat/index.html rather than /chat.html, so deep links work
      // when the export is served by a plain static file handler.
      trailingSlash: true,
      images: { unoptimized: true },
      experimental: {
        webpackMemoryOptimizations: true,
      },
    }
  : {
      // Required for the Docker standalone build — produces a self-contained
      // server.js that does not need node_modules at runtime.
      output: "standalone",
      experimental: {
        webpackMemoryOptimizations: true,
      },
    };

export default nextConfig;
