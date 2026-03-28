import { useState } from "react";

const BACKEND_URL = "http://127.0.0.1:8000";

function App() {
  const [repoUrl, setRepoUrl] = useState("");
  const [repoId, setRepoId] = useState("");
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function ingestRepo() {
    setLoading(true);
    setError("");
    setAnalysis(null);

    try {
      const res = await fetch(`${BACKEND_URL}/api/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl }),
      });

      const data = await res.json();
      setRepoId(data.repo_id);
      await fetchAnalysis(data.repo_id);
    } catch (err) {
      setError("Failed to ingest repository");
    } finally {
      setLoading(false);
    }
  }

  async function fetchAnalysis(id) {
    const res = await fetch(
      `${BACKEND_URL}/api/analysis/${id}`
    );
    const data = await res.json();
    setAnalysis(data.analysis);
  }

  return (
    <div className="min-h-screen bg-gray-100 p-8">
      <div className="max-w-4xl mx-auto bg-white p-6 rounded shadow">
        <h1 className="text-2xl font-bold mb-4">
          AI Engineering Copilot
        </h1>

        {/* Repo input */}
        <input
          className="border p-2 w-full mb-3"
          placeholder="Paste GitHub repo URL"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
        />

        <button
          className="bg-black text-white px-4 py-2 rounded"
          onClick={ingestRepo}
          disabled={loading}
        >
          {loading ? "Analyzing..." : "Analyze Repository"}
        </button>

        {error && (
          <p className="text-red-600 mt-3">{error}</p>
        )}

        {repoId && (
          <p className="text-sm text-gray-500 mt-3">
            Repo ID: {repoId}
          </p>
        )}

        {/* Analysis output */}
        {analysis && (
          <div className="mt-6 space-y-6">
            <Section title="📖 Reading Order" items={analysis.reading_order} />
            <Section title="📁 Folder Responsibilities" items={
              Object.entries(analysis.folder_responsibilities)
                .map(([k, v]) => `${k}: ${v}`)
            } />
            <Section title="🛠 First PR Ideas" items={analysis.first_pr_suggestions} />
            <Section title="⚠️ Code Smells" items={analysis.code_smells} />
            <Section title="♻️ Refactor Ideas" items={analysis.refactor_ideas} />
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, items }) {
  return (
    <div>
      <h2 className="font-semibold mb-2">{title}</h2>
      <ul className="list-disc ml-5 text-sm text-gray-700">
        {items.map((item, idx) => (
          <li key={idx}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export default App;
