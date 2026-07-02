"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

export default function SharedChatSessionPage() {
  const params = useParams<{ sessionId: string }>();
  const router = useRouter();

  useEffect(() => {
    const sessionId = params.sessionId;
    if (sessionId) {
      sessionStorage.setItem("k8s_pending_shared_session_id", sessionId);
    }
    router.replace("/chat");
  }, [params.sessionId, router]);

  return (
    <div className="flex h-screen items-center justify-center" style={{ background: "var(--bg-base)", color: "var(--text-primary)" }}>
      Loading shared chat session...
    </div>
  );
}
