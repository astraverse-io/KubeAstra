import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// The real token vocabulary the Mission Control components read, imported in
// place from the Next app so both build from one source (no drift). theme.css
// layers the webview surface on top.
import "@frontend/app/globals.css";
import "./theme.css";

// Mission Control's dark "cosmic" palette — the same theme the preview page uses.
document.documentElement.setAttribute("data-theme", "mission-control");

const el = document.getElementById("root");
if (el) {
  createRoot(el).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}
