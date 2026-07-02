"use client";

import { useState, useRef, useCallback } from "react";
import { ArrowRight, Check } from "lucide-react";

interface Props {
  onConfirm: () => void;
  label?: string;
  confirmedLabel?: string;
  disabled?: boolean;
}

export function SlideToConfirm({
  onConfirm,
  label = "Slide to confirm",
  confirmedLabel = "Confirmed",
  disabled = false,
}: Props) {
  const [dragOffset, setDragOffset] = useState(0);
  const [maxDragValue, setMaxDragValue] = useState(0);
  const [confirmed, setConfirmed] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);

  const measureMaxDrag = useCallback(() => {
    if (!containerRef.current || !thumbRef.current) return 0;
    const measured = containerRef.current.clientWidth - thumbRef.current.clientWidth - 8;
    const nextMax = Number.isFinite(measured) ? Math.max(0, measured) : 0;
    setMaxDragValue(nextMax);
    return nextMax;
  }, []);

  const safeMaxDrag = Math.max(0, maxDragValue);
  const dragProgress = safeMaxDrag > 0
    ? Math.max(0, Math.min(1, dragOffset / safeMaxDrag))
    : 0;

  const handlePointerDown = (e: React.PointerEvent) => {
    if (confirmed || disabled) return;
    setIsDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    if (!isDragging || confirmed || disabled) return;
    
    // Calculate movement within bounds
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || !thumbRef.current) return;
    
    // We get the pointer X relative to the container
    const max = measureMaxDrag();
    let newX = e.clientX - rect.left - thumbRef.current.clientWidth / 2;
    newX = Math.max(0, Math.min(newX, max));
    setDragOffset(newX);
  };

  const handlePointerUp = (e: React.PointerEvent) => {
    if (!isDragging || confirmed || disabled) return;
    setIsDragging(false);
    e.currentTarget.releasePointerCapture(e.pointerId);

    const max = measureMaxDrag();
    if (max > 0 && dragOffset >= max * 0.9) {
      // Trigger confirmation if reached 90%
      setDragOffset(max);
      setConfirmed(true);
      onConfirm();
    } else {
      // Snap back
      setDragOffset(0);
    }
  };

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "100%",
        height: "3rem",
        borderRadius: "9999px",
        background: confirmed ? "var(--brand-bg)" : "var(--paper-3)",
        border: "1px solid",
        borderColor: confirmed ? "var(--brand-bd)" : "var(--rule)",
        overflow: "hidden",
        userSelect: "none",
        opacity: disabled ? 0.6 : 1,
        transition: "all 0.3s ease",
      }}
    >
      {/* Background fill that follows the thumb */}
      <div
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          bottom: 0,
          width: `calc(${dragOffset}px + 3rem)`,
          background: "var(--brand-bg)",
          transition: isDragging ? "none" : "width 0.3s ease",
        }}
      />
      
      {/* Label Text */}
      <span
        style={{
          position: "relative",
          zIndex: 10,
          fontSize: "0.875rem",
          fontWeight: 500,
          color: confirmed ? "var(--brand)" : "var(--ink-2)",
          opacity: confirmed ? 1 : 1 - dragProgress,
          transition: "opacity 0.1s",
          pointerEvents: "none",
        }}
      >
        {confirmed ? confirmedLabel : label}
      </span>

      {/* Thumb / Knob */}
      <div
        ref={thumbRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        style={{
          position: "absolute",
          left: "4px",
          top: "4px",
          bottom: "4px",
          width: "2.5rem",
          borderRadius: "50%",
          background: confirmed ? "var(--brand)" : "var(--paper)",
          border: confirmed ? "none" : "1px solid var(--rule)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: confirmed ? "#000" : "var(--ink-3)",
          cursor: disabled || confirmed ? "default" : "grab",
          transform: `translateX(${dragOffset}px)`,
          transition: isDragging ? "none" : "transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), background 0.3s ease",
          boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
          zIndex: 20,
        }}
      >
        {confirmed ? <Check size={16} strokeWidth={3} /> : <ArrowRight size={16} />}
      </div>
    </div>
  );
}
