/**
 * API client — every backend call in one place.
 * The old App.jsx called endpoints that didn't exist (/api/ingest,
 * /api/analysis) — this client matches the real /api/v1 routes exactly.
 */

const BACKEND_URL = "http://127.0.0.1:8000";
const V1 = `${BACKEND_URL}/api/v1`;

async function asJson(res) {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/** GET /health — cheap reachability probe (used by the auto-reconnect loop). */
export function getHealth() {
  return fetch(`${BACKEND_URL}/health`).then(asJson);
}

/** POST /api/v1/ingest/ → { job_id } (202: work happens in the worker) */
export function triggerIngest(repoId, repoUrl) {
  return fetch(`${V1}/ingest/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_id: repoId, repo_url: repoUrl }),
  }).then(asJson);
}

/** GET /api/v1/ingest/status/{jobId} → { status: queued|processing|completed|failed } */
export function getJobStatus(jobId) {
  return fetch(`${V1}/ingest/status/${jobId}`).then(asJson);
}

/** GET /api/v1/analysis/{repoId} */
export function getAnalysis(repoId) {
  return fetch(`${V1}/analysis/${repoId}`).then(asJson);
}

/** GET /api/v1/graph/{repoId}/subgraph?... → { nodes, edges } */
export function getSubgraph(repoId, { center, file, kinds, depth, limit } = {}) {
  const params = new URLSearchParams();
  if (center) params.set("center", center);
  if (file) params.set("file", file);
  if (kinds?.length) params.set("kinds", kinds.join(","));
  params.set("depth", depth ?? 2);
  params.set("limit", limit ?? 500);
  return fetch(`${V1}/graph/${repoId}/subgraph?${params}`).then(asJson);
}

export function getGraphStats(repoId) {
  return fetch(`${V1}/graph/${repoId}/stats`).then(asJson);
}

/**
 * POST /api/v1/query/stream — Server-Sent Events over fetch.
 * The backend first sends {type:'sources'} (show files while the LLM thinks),
 * then many {type:'chunk'} tokens, then {type:'done'}.
 */
export async function streamQuery(repoId, query, { onSources, onChunk, onDone, onError }) {
  try {
    const res = await fetch(`${V1}/query/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_id: repoId, query }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n"); // SSE frames are blank-line separated
      buffer = events.pop(); // last piece may be incomplete — keep it
      for (const frame of events) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const payload = JSON.parse(line.slice(6));
        if (payload.type === "sources") onSources?.(payload.data);
        else if (payload.type === "chunk") onChunk?.(payload.data);
        else if (payload.type === "done") onDone?.();
        else if (payload.type === "error") onError?.(payload.data);
      }
    }
  } catch (err) {
    onError?.(err.message);
  }
}
