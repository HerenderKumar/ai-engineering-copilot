# The Frontend — React App with Chat, Analysis, and the 3D Graph

Vite + React (no framework beyond that). Four source files. The old `App.jsx` called endpoints that didn't exist (`/api/ingest`, `/api/analysis` — the backend serves `/api/v1/...`) and used Tailwind classes without Tailwind installed; both fixed.

## `src/api.js` — one place for every backend call

Small typed-ish client so components never build URLs:

- `triggerIngest(repoId, repoUrl)` → `POST /api/v1/ingest/` → `{job_id}`
- `getJobStatus(jobId)` → poll target
- `getAnalysis(repoId)`, `getSubgraph(repoId, {center, file, kinds, depth, limit})`, `getGraphStats(repoId)`
- `streamQuery(repoId, query, {onSources, onChunk, onDone, onError})` — the interesting one: reads the SSE stream via `fetch` + `ReadableStream` reader, accumulates a text buffer, splits on the blank-line frame delimiter (`\n\n`), keeps the trailing partial frame in the buffer, parses `data: {json}` lines and dispatches by `type`. This is how tokens appear word-by-word.

## `src/App.jsx` — the shell

State machine: enter Git URL → `triggerIngest` (repo_id auto-derived by slugifying the URL tail) → poll status every 2 s → badge walks `queued → processing → completed` → tabs unlock.

- **ChatTab** — message list + input. On send: append the user bubble and an empty assistant bubble, then `streamQuery` patches the last message: `onSources` fills the file-chip row (sources render BEFORE the answer — the "Reading: billing.py…" effect), `onChunk` appends tokens, `onDone` re-enables input. Auto-scrolls on new content.
- **AnalysisTab** — fetches once, renders each analysis section as a panel (strings → paragraph, lists → bullets, dicts → key/value list).
- **GraphView** — own file, below.

## `src/GraphView.jsx` — the 3D code map

Renderer: `react-force-graph-3d` (Three.js under the hood) — the plan's v1 choice (v2 = custom `InstancedMesh` when node counts demand it).

**The performance trick (§6.9 lever #1):** positions were precomputed server-side, so every node is **pinned** (`fx/fy/fz = x/y/z from the API`) and `cooldownTicks={0}` — the browser runs *zero* physics; it only draws. This is what keeps thousands of nodes smooth.

The visual encodings:
- **node color = Louvain cluster** (10-color palette) — subsystems appear as colored "galaxies" (LOD lever #2 foundation)
- **node size = kind** (files > classes > functions)
- **edge color = confidence overlay**: `< 0.5` → thin red — the graph-QA surface; resolver mistakes are literally visible
- arrows show call/import direction; hover tooltip = qualified name + `file:line`

Controls: edge-kind checkboxes (CONTAINS/CALLS/IMPORTS/INHERITS), depth select (1-4), stats line with the low-confidence count. **Click a node** → neighborhood view re-centered on it (`center=` param) + a detail card (qualified name, kind, `file:start–end`, signature); "← full overview" returns.

## `src/App.css` + `src/index.css`

Self-contained dark theme (no CSS framework): topbar/ingest row, status badges (colored per state), tab bar, chat bubbles + source chips, analysis panels, graph toolbar/canvas/node card. `index.css` is a minimal reset + dark background so the WebGL canvas blends in.

## Rebuild order

`api.js` first (test with curl-by-hand equivalence) → `App.jsx` shell with ingest + polling → ChatTab (SSE parsing is the only tricky part — get the frame-buffer loop right) → AnalysisTab (trivial) → `npm i react-force-graph-3d` → GraphView. Checkpoint: index a small repo end-to-end, ask one question, rotate the graph, click a node.
