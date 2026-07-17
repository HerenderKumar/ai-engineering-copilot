/**
 * 3D knowledge-graph view (Phase 3, handoff §6.9).
 *
 * Performance model: the SERVER precomputed x/y/z (igraph/networkx) and
 * Louvain clusters at index time, so we pin every node (fx/fy/fz) and set
 * cooldownTicks=0 — the browser runs ZERO physics, it only renders. That's
 * how this stays smooth on thousands of nodes.
 *
 * Views: architecture overview (whole bounded subgraph, colored by cluster
 * "galaxies"), neighborhood (click a node → re-center N hops around it),
 * and the CONFIDENCE OVERLAY — low-confidence edges (< 0.5) render red and
 * thin: that's the graph-QA surface where resolver mistakes are visible.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { getSubgraph } from "./api";

const CLUSTER_COLORS = [
  "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
  "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
];
const KIND_SIZES = { file: 6, class: 4.5, method: 2.5, function: 2.5 };
const ALL_KINDS = ["CONTAINS", "CALLS", "IMPORTS", "INHERITS"];

export default function GraphView({ repoId }) {
  const containerRef = useRef(null);
  const fgRef = useRef(null);
  const [data, setData] = useState({ nodes: [], links: [] });
  const [kinds, setKinds] = useState(["CALLS", "IMPORTS", "INHERITS"]);
  const [depth, setDepth] = useState(2);
  const [center, setCenter] = useState(null); // node id → neighborhood view
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [size, setSize] = useState({ width: 800, height: 560 });

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() =>
      setSize({ width: el.clientWidth, height: 560 }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const load = useCallback(async () => {
    setError("");
    try {
      const sub = await getSubgraph(repoId, {
        center: center ?? undefined,
        kinds,
        depth,
        limit: 600,
      });
      setData({
        nodes: sub.nodes.map((n) => ({
          ...n,
          // Pin server-computed positions: no browser physics.
          fx: n.x ?? 0, fy: n.y ?? 0, fz: n.z ?? 0,
        })),
        links: sub.edges.map((e) => ({
          source: e.src, target: e.dst, kind: e.kind, confidence: e.confidence,
        })),
      });
    } catch (err) {
      setError(err.message);
      setData({ nodes: [], links: [] });
    }
  }, [repoId, kinds, depth, center]);

  useEffect(() => { load(); }, [load]);

  // Positions are precomputed server-side and pinned, and cooldownTicks=0
  // means the engine never runs — so the camera must be fitted explicitly,
  // or the node cloud sits tiny/off-frame and the view looks blank.
  useEffect(() => {
    if (!data.nodes.length || !fgRef.current) return;
    const t = setTimeout(() => fgRef.current.zoomToFit(500, 60), 150);
    return () => clearTimeout(t);
  }, [data]);

  const stats = useMemo(() => ({
    nodes: data.nodes.length,
    edges: data.links.length,
    lowConf: data.links.filter((l) => l.confidence < 0.5).length,
  }), [data]);

  function toggleKind(kind) {
    setKinds((prev) =>
      prev.includes(kind) ? prev.filter((k) => k !== kind) : [...prev, kind]);
  }

  return (
    <div className="graph-view">
      <div className="graph-toolbar">
        {ALL_KINDS.map((kind) => (
          <label key={kind} className="kind-toggle">
            <input type="checkbox" checked={kinds.includes(kind)}
                   onChange={() => toggleKind(kind)} />
            {kind}
          </label>
        ))}
        <label className="kind-toggle">
          depth
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[1, 2, 3, 4].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        {center && (
          <button className="btn small" onClick={() => setCenter(null)}>
            ← full overview
          </button>
        )}
        <span className="graph-stats">
          {stats.nodes} nodes · {stats.edges} edges ·{" "}
          <span className="low-conf">{stats.lowConf} low-confidence</span>
        </span>
      </div>

      {error && <p className="error">{error}</p>}

      <div className="graph-canvas" ref={containerRef}>
        <ForceGraph3D
          ref={fgRef}
          graphData={data}
          width={size.width}
          height={size.height}
          backgroundColor="#0b0e14"
          cooldownTicks={0}
          enableNodeDrag={false}
          nodeId="id"
          nodeLabel={(n) => `${n.qualified_name} (${n.kind})<br/>${n.file}:${n.start_line ?? "?"}`}
          nodeColor={(n) => CLUSTER_COLORS[(n.cluster ?? 0) % CLUSTER_COLORS.length]}
          nodeVal={(n) => KIND_SIZES[n.kind] ?? 2}
          nodeOpacity={0.92}
          linkColor={(l) => (l.confidence < 0.5 ? "#e15759" : "#8899aa")}
          linkOpacity={0.35}
          linkWidth={(l) => (l.confidence < 0.5 ? 0.4 : 1)}
          linkDirectionalArrowLength={2.5}
          linkDirectionalArrowRelPos={1}
          onNodeClick={(n) => { setSelected(n); setCenter(n.id); }}
        />
      </div>

      {selected && (
        <div className="node-card">
          <strong>{selected.qualified_name}</strong>
          <span className="badge">{selected.kind}</span>
          <div className="mono">
            {selected.file}:{selected.start_line}–{selected.end_line}
          </div>
          {selected.signature && <code>{selected.signature}</code>}
        </div>
      )}
    </div>
  );
}
