import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The Mission Control components live in the Next.js app and are imported here
// IN PLACE — no copy, no published package — so both apps build from one source
// tree (the "importable by both" requirement). See the plan for why.
const frontend = fileURLToPath(new URL("../../../ui/frontend", import.meta.url));
// The webview's own React copy. The Mission Control components live under
// ui/frontend and would otherwise resolve `react` from ui/frontend/node_modules
// — a SECOND, differently-versioned copy. Two React copies break hooks
// ("useState of null"). Aliasing react/react-dom to the webview's single copy
// (subpaths like react/jsx-runtime resolve under it too) forces one instance.
const react19 = fileURLToPath(new URL("./node_modules/react", import.meta.url));
const reactDom = fileURLToPath(new URL("./node_modules/react-dom", import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@frontend": frontend,
      "@components": `${frontend}/components`,
      "@lib": `${frontend}/lib`,
      react: react19,
      "react-dom": reactDom,
    },
    dedupe: ["react", "react-dom"],
  },
  // Safety net: some frontend modules read Next's build-time env. The webview
  // never uses their runtime fetch (it goes through the host bridge), but a
  // transitive import must not crash on an undefined `process`.
  define: {
    "process.env.NEXT_PUBLIC_API_URL": '""',
    "process.env.NODE_ENV": '"production"',
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Deterministic names so panel.ts can reference index.js / index.css.
    rollupOptions: {
      output: {
        entryFileNames: "index.js",
        assetFileNames: "index.[ext]",
        chunkFileNames: "[name].js",
      },
    },
  },
});
