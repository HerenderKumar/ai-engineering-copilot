/**
 * App shell — one engine, three views:
 *   Chat     : streaming GraphRAG Q&A (sources arrive first, then tokens)
 *   Analysis : the beginner-friendly repo explanation layer
 *   Graph    : the 3D code knowledge graph
 *
 * Flow: enter a git URL → POST /ingest (202 + job_id) → poll job status →
 * when completed, all three tabs light up. The old App called endpoints that
 * didn't exist; every call now goes through src/api.js.
 */

import { useEffect, useRef, useState } from "react";
import {
  getAnalysis, getGraphStats, getHealth, getJobStatus, streamQuery, triggerIngest,
} from "./api";
import GraphView from "./GraphView";
import "./App.css";

function slugFromUrl(url) {
  try {
    const tail = url.split("/").filter(Boolean).pop() || "repo";
    return tail.replace(/\.git$/, "").toLowerCase().replace(/[^a-z0-9-_]/g, "-");
  } catch {
    return "repo";
  }
}

export default function App() {
  const [repoUrl, setRepoUrl] = useState("");
  const [repoId, setRepoId] = useState("");
  const [jobState, setJobState] = useState(null); // queued | processing | completed | failed
  const [error, setError] = useState("");
  const [tab, setTab] = useState("chat");
  const [backendUp, setBackendUp] = useState(true);
  const pollRef = useRef(null);

  async function ingest() {
    setError("");
    const id = slugFromUrl(repoUrl);
    try {
      const res = await triggerIngest(id, repoUrl);
      setRepoId(id);
      setJobState("queued");
      clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const s = await getJobStatus(res.job_id);
          setJobState(s.status);
          if (s.status === "completed" || s.status === "failed") {
            clearInterval(pollRef.current);
            if (s.status === "failed") setError(s.error || "Ingestion failed");
            else localStorage.setItem("copilot.lastRepo",
                                      JSON.stringify({ repoId: id, repoUrl }));
          }
        } catch { /* transient poll errors are fine */ }
      }, 2000);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => () => clearInterval(pollRef.current), []);

  // Restore the last indexed repo after a page refresh — and NEVER brick.
  // The old version checked the backend exactly ONCE at load; if Docker was
  // down or the API still starting (common on this machine), the app locked
  // every tab forever with no message — the "blank black page". Now: keep
  // retrying with a visible banner until the backend answers, and unlock the
  // moment it does (also re-check when the window regains focus).
  useEffect(() => {
    let stopped = false;
    let timer = null;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("copilot.lastRepo") || "null"); } catch { /* corrupted — ignore */ }

    async function attempt() {
      try {
        await getHealth();
        if (stopped) return true;
        setBackendUp(true);
        if (saved?.repoId) {
          const s = await getGraphStats(saved.repoId); // trust storage only if really indexed
          if (!stopped && s?.nodes > 0) {
            setRepoId(saved.repoId);
            if (saved.repoUrl) setRepoUrl(saved.repoUrl);
            setJobState("completed");
          }
        }
        return true; // backend reachable — stop polling
      } catch {
        if (!stopped) setBackendUp(false);
        return false;
      }
    }

    const loop = async () => {
      if (await attempt() || stopped) return;
      timer = setTimeout(loop, 3000);
    };
    loop();
    const onFocus = () => { if (!stopped) attempt(); };
    window.addEventListener("focus", onFocus);
    return () => { stopped = true; clearTimeout(timer); window.removeEventListener("focus", onFocus); };
  }, []);

  const ready = jobState === "completed";

  return (
    <div className="shell">
      <header className="topbar">
        <h1>AI Engineering Copilot</h1>
        <div className="ingest-row">
          <input
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/owner/repo.git"
            onKeyDown={(e) => e.key === "Enter" && ingest()}
          />
          <button className="btn" onClick={ingest}
                  disabled={!repoUrl || jobState === "processing" || jobState === "queued"}>
            {jobState === "processing" || jobState === "queued" ? "Indexing…" : "Index repo"}
          </button>
        </div>
        {repoId && (
          <div className="status-row">
            <span className="mono">{repoId}</span>
            {jobState && <span className={`badge ${jobState}`}>{jobState}</span>}
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {!backendUp && (
          <p className="error">⚠ Backend unreachable at 127.0.0.1:8000 — is Docker running?
            Reconnecting automatically…</p>
        )}
      </header>

      <nav className="tabs">
        {["chat", "analysis", "graph"].map((t) => (
          <button key={t}
                  className={`tab ${tab === t ? "active" : ""}`}
                  onClick={() => setTab(t)}>
            {t === "chat" ? "💬 Chat" : t === "analysis" ? "📖 Analysis" : "🕸 Graph"}
          </button>
        ))}
        {!ready && <span className="hint">
          {backendUp ? "index a repository to unlock the views" : "waiting for the backend…"}
        </span>}
      </nav>

      <main>
        {ready && tab === "chat" && <ChatTab repoId={repoId} />}
        {ready && tab === "analysis" && <AnalysisTab repoId={repoId} />}
        {ready && tab === "graph" && <GraphView repoId={repoId} />}
        {!ready && (
          <div className="chat"><div className="chat-log">
            {backendUp
              ? <p className="hint">Nothing here yet — paste a git URL above and press
                  “Index repo”. When indexing completes (or your last repo is restored),
                  Chat, Analysis and Graph light up automatically.</p>
              : <p className="error">⚠ The backend isn’t reachable right now. Start Docker
                  Desktop (or run ./start.sh in the project folder) — this page will unlock
                  itself the moment the backend answers. No reload needed.</p>}
          </div></div>
        )}
      </main>
    </div>
  );
}

/* ---------------------------------------------------------------- Chat --- */

function ChatTab({ repoId }) {
  const [messages, setMessages] = useState([]); // {role, text, sources?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [messages]);

  async function send() {
    const query = input.trim();
    if (!query || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: query },
                              { role: "assistant", text: "", sources: [] }]);

    const patchLast = (fn) => setMessages((m) => {
      const copy = [...m];
      copy[copy.length - 1] = fn(copy[copy.length - 1]);
      return copy;
    });

    await streamQuery(repoId, query, {
      onSources: (sources) => patchLast((msg) => ({ ...msg, sources })),
      onChunk: (token) => patchLast((msg) => ({ ...msg, text: msg.text + token })),
      onDone: () => setBusy(false),
      onError: (err) => { patchLast((msg) => ({ ...msg, text: msg.text + `\n[error: ${err}]` })); setBusy(false); },
    });
  }

  return (
    <div className="chat">
      <div className="chat-log">
        {messages.length === 0 && (
          <p className="hint">Ask anything about the codebase — "how does ingestion
          avoid re-embedding unchanged files?", "what calls the reranker?"…</p>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`bubble ${msg.role}`}>
            {msg.sources?.length > 0 && (
              <div className="sources">
                {msg.sources.map((s, j) => (
                  <span key={j} className="chip mono">{s.file_path}</span>
                ))}
              </div>
            )}
            <div className="bubble-text">{msg.text || (msg.role === "assistant" && busy
              ? "thinking… (local model — first words can take ~15 s)" : "")}</div>
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <div className="chat-input">
        <input value={input} onChange={(e) => setInput(e.target.value)}
               placeholder="Ask about the codebase…"
               onKeyDown={(e) => e.key === "Enter" && send()} />
        <button className="btn" onClick={send} disabled={busy || !input.trim()}>Send</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ Analysis --- */

function AnalysisTab({ repoId }) {
  const [analysis, setAnalysis] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getAnalysis(repoId)
      .then((data) => setAnalysis(data.analysis))
      .catch((err) => setError(err.message));
  }, [repoId]);

  if (error) return <p className="error">{error}</p>;
  if (!analysis) return <p className="hint">Analyzing…</p>;

  const render = (value) => {
    if (Array.isArray(value)) return <ul>{value.map((v, i) => <li key={i}>{String(v)}</li>)}</ul>;
    if (value && typeof value === "object")
      return <ul>{Object.entries(value).map(([k, v]) => <li key={k}><b>{k}</b>: {String(v)}</li>)}</ul>;
    return <p>{String(value)}</p>;
  };

  const titles = {
    project_summary: "📦 Project summary",
    architecture: "🏛 Architecture",
    entry_points: "🚪 Entry points",
    folder_responsibilities: "📁 Folder responsibilities",
    reading_order: "📖 Suggested reading order",
    safe_contributions: "🌱 Safe first contributions",
    feature_flow: "🔀 Feature flow",
  };

  return (
    <div className="analysis">
      {Object.entries(titles).map(([key, title]) =>
        analysis[key] ? (
          <section key={key} className="panel">
            <h2>{title}</h2>
            {render(analysis[key])}
          </section>
        ) : null)}
    </div>
  );
}
