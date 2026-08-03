"use client";

/**
 * The header's set-once drawer: theme, settings, account, sign out.
 *
 * Deliberately narrow. An earlier sketch of this header put *everything*
 * behind here, which is not hierarchy — it is concealment, and it would have
 * put Alerts two clicks away in a tool people open during incidents. The rule
 * that decides membership: if you might reach for it while something is
 * broken, it stays in the bar.
 */

import React, { useEffect, useRef } from "react";

export type OverflowItem = {
  label: string;
  onSelect: () => void;
  icon?: React.ReactNode;
  /** Starts a new group above this item. */
  group?: string;
};

export default function HeaderOverflow({
  open,
  onOpenChange,
  items,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: OverflowItem[];
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const firstItemRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    firstItemRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onOpenChange(false);
        // Return focus to the trigger, or the menu closes into nowhere for
        // anyone driving this from the keyboard.
        wrapRef.current?.querySelector<HTMLButtonElement>(".ka-overflow-trigger")?.focus();
      }
    };
    const onPointer = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) onOpenChange(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open, onOpenChange]);

  return (
    <div className="ka-overflow" ref={wrapRef}>
      <button
        type="button"
        className="ka-overflow-trigger"
        onClick={() => onOpenChange(!open)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More"
        title="More"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.6" />
          <circle cx="12" cy="12" r="1.6" />
          <circle cx="19" cy="12" r="1.6" />
        </svg>
      </button>

      {open && (
        <div className="ka-overflow-menu" role="menu">
          {items.map((item, i) => (
            <React.Fragment key={item.label}>
              {item.group && <div className="ka-overflow-group">{item.group}</div>}
              <button
                type="button"
                role="menuitem"
                ref={i === 0 ? firstItemRef : undefined}
                className="ka-overflow-item"
                onClick={() => {
                  onOpenChange(false);
                  item.onSelect();
                }}
              >
                {item.icon}
                {item.label}
              </button>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
