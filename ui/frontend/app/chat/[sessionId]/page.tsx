"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

/**
 * Back-compat for shared links in the old `/chat/<id>` shape.
 *
 * Share links are now `/chat?session=<id>`. A dynamic route cannot exist in
 * the desktop static export — it would need `generateStaticParams()`, and
 * session ids are not knowable at build time — so the query form is the one
 * shape that works in both builds.
 *
 * This page therefore ships only in the SERVER build; the desktop build
 * stashes it aside (ui/frontend/scripts/build-desktop.mjs). It forwards to
 * the query form so links shared before the change keep resolving.
 */
export default function SharedChatSessionPage() {
  const params = useParams<{ sessionId: string }>();
  const router = useRouter();

  useEffect(() => {
    const sessionId = params.sessionId;
    router.replace(
      sessionId ? `/chat?session=${encodeURIComponent(sessionId)}` : "/chat",
    );
  }, [params.sessionId, router]);

  return (
    <div className="flex h-screen items-center justify-center" style={{ background: "var(--bg-base)", color: "var(--text-primary)" }}>
      Loading shared chat session...
    </div>
  );
}
