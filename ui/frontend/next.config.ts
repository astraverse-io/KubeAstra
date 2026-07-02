import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required for the Docker standalone build — produces a self-contained
  // server.js that does not need node_modules at runtime.
  output: "standalone",
  experimental: {
    webpackMemoryOptimizations: true,
  },
};

export default nextConfig;
