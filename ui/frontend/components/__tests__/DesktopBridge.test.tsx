import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DesktopBridge,
  investigationPrompt,
  parseDesktopHash,
} from "../DesktopBridge";

function setHash(hash: string) {
  window.history.replaceState(null, "", `/chat/${hash}`);
}

afterEach(() => {
  window.history.replaceState(null, "", "/chat/");
});

describe("parseDesktopHash", () => {
  it("ignores an empty or unrelated fragment", () => {
    expect(parseDesktopHash("")).toBeNull();
    expect(parseDesktopHash("#")).toBeNull();
    expect(parseDesktopHash("#section-2")).toBeNull();
    // The splash page's failure fragment must not be mistaken for a request.
    expect(parseDesktopHash("#fail=%7B%22headline%22%3A%22x%22%7D")).toBeNull();
  });

  it("reads the focus request the global shortcut sends", () => {
    expect(parseDesktopHash("#kubeastra=focus&n=123")).toEqual({ action: "focus" });
  });

  it("reads a deep link's namespace and pod", () => {
    expect(parseDesktopHash("#kubeastra=investigate&ns=prod&pod=api-0&n=1")).toEqual({
      action: "investigate",
      namespace: "prod",
      pod: "api-0",
    });
  });

  it("percent-decodes values, since the shell encodes every one", () => {
    expect(parseDesktopHash("#kubeastra=investigate&ns=kube%2Dsystem&pod=a%20b")).toEqual({
      action: "investigate",
      namespace: "kube-system",
      pod: "a b",
    });
  });

  it("rejects an unknown action rather than guessing", () => {
    expect(parseDesktopHash("#kubeastra=rm-rf&n=1")).toBeNull();
  });
});

describe("investigationPrompt", () => {
  it("names both when both are known", () => {
    expect(investigationPrompt("prod", "api-0")).toBe(
      "Investigate pod api-0 in namespace prod",
    );
  });

  it("falls back to whichever it has", () => {
    expect(investigationPrompt(undefined, "api-0")).toBe("Investigate pod api-0");
    expect(investigationPrompt("prod")).toBe(
      "Investigate what is wrong in namespace prod",
    );
  });

  it("is empty when it has nothing to go on", () => {
    expect(investigationPrompt()).toBe("");
  });

  // Regression, both halves observed against a running backend:
  //   "…in namespace prod."  -> Invalid namespace name: 'prod.'
  //   "…in namespace `prod`" -> Invalid namespace name: '`prod`'
  // Tool arguments are the literal trailing token, so nothing may touch it.
  it("leaves the trailing identifier bare and unpunctuated", () => {
    for (const [ns, pod] of [
      ["prod", "api-0"],
      [undefined, "api-0"],
      ["prod", undefined],
    ] as const) {
      const prompt = investigationPrompt(ns, pod);
      const last = prompt.split(" ").pop() ?? "";
      expect(last).toMatch(/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/);
      expect(last).toBe(pod && !ns ? pod : ns);
    }
  });

  it("keeps hyphenated names intact", () => {
    expect(investigationPrompt("kube-system", "etcd-0")).toBe(
      "Investigate pod etcd-0 in namespace kube-system",
    );
  });
});

describe("DesktopBridge", () => {
  it("acts on a fragment already present at mount (cold start via deep link)", () => {
    const onInvestigate = vi.fn();
    setHash("#kubeastra=investigate&ns=prod&pod=api-0");
    render(<DesktopBridge onInvestigate={onInvestigate} />);
    expect(onInvestigate).toHaveBeenCalledWith("Investigate pod api-0 in namespace prod");
  });

  it("clears the fragment so a reload does not replay the request", () => {
    setHash("#kubeastra=focus");
    render(<DesktopBridge onInvestigate={vi.fn()} onFocus={vi.fn()} />);
    expect(window.location.hash).toBe("");
  });

  it("focuses on a focus request", () => {
    const onFocus = vi.fn();
    setHash("#kubeastra=focus&n=1");
    render(<DesktopBridge onInvestigate={vi.fn()} onFocus={onFocus} />);
    expect(onFocus).toHaveBeenCalled();
  });

  it("responds every time, because the shortcut can be pressed repeatedly", () => {
    const onFocus = vi.fn();
    render(<DesktopBridge onInvestigate={vi.fn()} onFocus={onFocus} />);

    for (const nonce of [1, 2, 3]) {
      window.history.replaceState(null, "", `/chat/#kubeastra=focus&n=${nonce}`);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    }
    expect(onFocus).toHaveBeenCalledTimes(3);
  });

  it("falls back to focusing when a deep link carries no target", () => {
    const onFocus = vi.fn();
    const onInvestigate = vi.fn();
    setHash("#kubeastra=investigate&n=1");
    render(<DesktopBridge onInvestigate={onInvestigate} onFocus={onFocus} />);
    expect(onInvestigate).not.toHaveBeenCalled();
    expect(onFocus).toHaveBeenCalled();
  });

  it("does nothing without a fragment, which is server mode", () => {
    const onInvestigate = vi.fn();
    const onFocus = vi.fn();
    render(<DesktopBridge onInvestigate={onInvestigate} onFocus={onFocus} />);
    expect(onInvestigate).not.toHaveBeenCalled();
    expect(onFocus).not.toHaveBeenCalled();
  });
});
