import { useCallback, useEffect, useRef, useState } from "react";

const STICK_THRESHOLD_PX = 80;

/**
 * Keep an element pinned to the bottom on new content ONLY while the user
 * is already at (or near) the bottom. Once the user scrolls up past
 * STICK_THRESHOLD_PX, we stop auto-scrolling so they can read prior context
 * without being yanked back down.
 *
 * Usage:
 *   const { ref, scrollToBottom, isSticky } = useStickyBottom<HTMLDivElement>([messages]);
 *   <main ref={ref}>...</main>
 *   {!isSticky && <button onClick={scrollToBottom}>Jump to latest ↓</button>}
 */
export function useStickyBottom<T extends HTMLElement>(deps: React.DependencyList) {
  const ref = useRef<T | null>(null);
  const stickyRef = useRef(true);
  const [isSticky, setIsSticky] = useState(true);

  const setSticky = useCallback((next: boolean) => {
    if (stickyRef.current !== next) {
      stickyRef.current = next;
      setIsSticky(next);
    }
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const el = ref.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    setSticky(true);
  }, [setSticky]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      setSticky(distance <= STICK_THRESHOLD_PX);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [setSticky]);

  useEffect(() => {
    if (stickyRef.current && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ref, scrollToBottom, isSticky };
}
