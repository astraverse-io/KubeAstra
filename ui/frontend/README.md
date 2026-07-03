# Frontend

Next.js frontend for the KubeAstra UI.

This app provides the browser-based chat interface and now includes a **server-side API proxy** so browser requests go to same-origin `/api/*` routes on port `3000` instead of talking directly to the backend on port `8000`.

## What Changed

- Added `app/api/[...path]/route.ts` as a runtime proxy to the FastAPI backend
- `lib/api.ts` now defaults to same-origin `/api/*` calls
- The frontend uses `API_BASE_URL` at runtime instead of relying on a baked-in `NEXT_PUBLIC_API_URL`
- Production builds no longer depend on embedding the final backend URL into the browser bundle
- The legacy form dashboard API client was restored so `npm run build` succeeds
- **Streaming chat** — `sendChatStream` in `lib/api.ts` reads the backend's `/api/chat/stream` SSE feed via `fetch` + `ReadableStream`. The chat page accumulates live `iteration_planned` / `step_complete` events into the active message (real ReAct pills replace the static placeholder list) and appends token-by-token text from `answer_start` / `token` / `answer_end` events so the final answer streams in word-by-word like ChatGPT.
- **Thumbs-up / thumbs-down feedback** (Phase 1.3) — the chat page renders 👍 / 👎 buttons under completed assistant answers. If the backend captured the answer into RAG `session_memory`, the response has a `capture_id` and feedback promotes/quarantines that entry. If capture was skipped or Qdrant is unavailable, feedback is still persisted as an audit-only SQLite event. Feedback sends the nearest user prompt and assistant answer text for beta review, but does not send the raw Kubernetes result payload.
- **Local account auth** — when the backend has `AUTH_ENABLED=true`, the chat page shows a local login/signup panel and loads account-owned sessions from `/api/sessions`, so the same user sees chat history across browsers. When auth is disabled, the existing anonymous `localStorage` session behavior remains.

## Runtime Flow

```text
Browser
  -> http://localhost:3000/api/...
  -> Next.js route handler (app/api/[...path]/route.ts)
  -> API_BASE_URL + /api/...
  -> FastAPI backend on port 8000
```

## Local Development

From the repo root:

```bash
cd ui/frontend
API_BASE_URL=http://localhost:8000 npm run dev
```

Open:

- `http://localhost:3000/chat`

The browser will call:

- `http://localhost:3000/api/chat`
- `http://localhost:3000/api/sessions/...`
- `http://localhost:3000/api/health`

The Next.js server then proxies those requests to the backend defined by `API_BASE_URL`.

## Environment Variables

```bash
# Server-side proxy target
API_BASE_URL=http://localhost:8000
```

Notes:

- `API_BASE_URL` is read by the Next.js server, not by the browser
- This means you can change the backend target at runtime without rebuilding the frontend image
- `NEXT_PUBLIC_API_URL` is no longer required for the normal app flow

## Important Files

- [app/chat/page.tsx](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/frontend/app/chat/page.tsx)
  Main chat UI
- [app/api/[...path]/route.ts](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/frontend/app/api/[...path]/route.ts)
  Runtime backend proxy
- [lib/api.ts](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/frontend/lib/api.ts)
  Typed API client and legacy dashboard client shim
- [Dockerfile](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/frontend/Dockerfile)
  Frontend image build

## Verification

```bash
npm run lint
npm run build
```

Expected:

- lint passes
- production build succeeds
- `/api/[...path]` is listed as a dynamic route in the Next.js build output
