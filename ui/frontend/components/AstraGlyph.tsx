import React from "react";

type AstraGlyphProps = {
  size?: number;
  animate?: boolean;
  title?: string;
};

export function AstraGlyph({ size = 22, animate = false, title = "KubeAstra" }: AstraGlyphProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      role="img"
      aria-label={title}
      style={{
        animation: animate ? "mcGlow 2s ease-in-out infinite" : "none",
        display: "block",
      }}
    >
      <title>{title}</title>
      <polygon
        points="12,2 21,7 21,17 12,22 3,17 3,7"
        fill="none"
        stroke="var(--cyan, var(--brand))"
        strokeWidth="0.8"
        opacity="0.5"
      />
      <path
        d="M12 4.5 L13.4 10.6 L19.5 12 L13.4 13.4 L12 19.5 L10.6 13.4 L4.5 12 L10.6 10.6 Z"
        fill="var(--cyan, var(--brand))"
      />
      <circle cx="12" cy="12" r="1.3" fill="var(--bg-0, var(--paper))" />
    </svg>
  );
}

export default AstraGlyph;
