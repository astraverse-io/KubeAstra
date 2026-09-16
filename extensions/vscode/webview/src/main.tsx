import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./theme.css";

const el = document.getElementById("root");
if (el) {
  createRoot(el).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}
