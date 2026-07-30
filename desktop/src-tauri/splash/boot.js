// Renders a startup failure into the splash window.
//
// The Rust side reports failures by navigating this page to `#fail=<encoded>`
// rather than by injecting script. That choice is about the CSP in
// tauri.conf.json: `default-src 'self'` with no `script-src` blocks inline
// script, and whether a host-side `eval()` is exempt depends on the platform
// webview. A same-origin file loaded by <script src> is allowed everywhere,
// and a hash change is navigation, not execution — so this path cannot be
// silently blocked.
//
// Both `hashchange` and initial load are handled: the failure usually arrives
// while this page is already open, which is a same-document change.

(function () {
  function render() {
    var raw = location.hash.replace(/^#fail=/, "");
    if (!raw || raw === location.hash) return;

    var payload;
    try {
      payload = JSON.parse(decodeURIComponent(raw));
    } catch (e) {
      payload = { headline: "KubeAstra could not start", detail: String(raw) };
    }

    var root = document.getElementById("root");
    if (!root) return;

    root.setAttribute("data-state", "error");
    document.body.setAttribute("data-state", "error");

    var headline = document.getElementById("headline");
    var detail = document.getElementById("detail");
    if (headline) headline.textContent = payload.headline || "KubeAstra could not start";
    if (detail) detail.textContent = "KubeAstra could not start its local backend.";

    var existing = root.querySelector("pre");
    if (existing) existing.remove();
    if (payload.detail) {
      var pre = document.createElement("pre");
      pre.textContent = payload.detail;
      root.appendChild(pre);
    }
  }

  window.addEventListener("hashchange", render);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
