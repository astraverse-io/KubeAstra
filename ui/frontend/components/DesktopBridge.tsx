"use client";

import { useEffect } from "react";

/**
 * Receives requests from the desktop shell.
 *
 * The Tauri shell — tray menu, the global shortcut, and `kubeastra://` deep
 * links — steers the app by navigating to `#kubeastra=<action>&…` rather than
 * over Tauri IPC. Two reasons: the app is served from http://127.0.0.1:<port>,
 * a remote origin Tauri only exposes IPC to after explicitly opting it in; and
 * a fragment-only change does not reload the page, so pressing the shortcut
 * during an investigation cannot discard it.
 *
 * Renders nothing. In server mode the fragment never appears and this is inert.
 */

export type DesktopRequest =
  | { action: "focus" }
  | { action: "investigate"; namespace?: string; pod?: string };

/** Exported for tests: turn a location.hash into a request, or null. */
export function parseDesktopHash(hash: string): DesktopRequest | null {
  const raw = hash.replace(/^#/, "");
  if (!raw) return null;

  const params = new URLSearchParams(raw);
  const action = params.get("kubeastra");
  if (!action) return null;

  if (action === "focus") return { action: "focus" };
  if (action === "investigate") {
    return {
      action: "investigate",
      namespace: params.get("ns") || undefined,
      pod: params.get("pod") || undefined,
    };
  }
  return null;
}

/**
 * Exported for tests: the prompt a deep link turns into.
 *
 * Identifiers appear bare and last, with nothing attached to them — no
 * trailing punctuation and no quoting. Tool arguments are extracted as the
 * literal token, so anything adjacent becomes part of the name:
 *
 *   "…in namespace prod."    -> Invalid namespace name: 'prod.'
 *   "…in namespace `prod`"   -> Invalid namespace name: '`prod`'
 *
 * Both were observed against a running backend by opening a real
 * kubeastra:// link. Keep identifiers naked and sentence-final.
 */
export function investigationPrompt(namespace?: string, pod?: string): string {
  if (pod && namespace) return `Investigate pod ${pod} in namespace ${namespace}`;
  if (pod) return `Investigate pod ${pod}`;
  if (namespace) return `Investigate what is wrong in namespace ${namespace}`;
  return "";
}

/** Focus whichever input bar is mounted — Mission Control or the intent bar. */
export function focusCommandInput(): boolean {
  const element = document.querySelector<HTMLElement>("[data-kubeastra-input]");
  if (!element) return false;
  element.focus();
  return true;
}

type Props = {
  onInvestigate: (prompt: string) => void;
  /** Overridable so tests do not need a real input in the DOM. */
  onFocus?: () => void;
};

export function DesktopBridge({ onInvestigate, onFocus }: Props) {
  useEffect(() => {
    function handle() {
      const request = parseDesktopHash(window.location.hash);
      if (!request) return;

      // Clear the fragment so a reload does not replay the request, and so
      // the next identical one still fires hashchange. replaceState keeps it
      // out of history — the user never navigated here.
      window.history.replaceState(null, "", window.location.pathname + window.location.search);

      if (request.action === "focus") {
        (onFocus ?? focusCommandInput)();
        return;
      }

      const prompt = investigationPrompt(request.namespace, request.pod);
      if (prompt) {
        onInvestigate(prompt);
      } else {
        // A deep link with no usable target still deserves to surface the app
        // with the cursor ready, rather than doing nothing at all.
        (onFocus ?? focusCommandInput)();
      }
    }

    window.addEventListener("hashchange", handle);
    handle(); // a cold start arrives with the fragment already in place
    return () => window.removeEventListener("hashchange", handle);
  }, [onInvestigate, onFocus]);

  return null;
}
