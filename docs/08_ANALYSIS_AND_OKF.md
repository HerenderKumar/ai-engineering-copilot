# The Analysis Layer and OKF — The "Why" Layer

Code answers "what"; this layer captures "why and where to start". It's also the foundation for the recommended product wedge (onboarding & codebase comprehension).

## `app/services/analysis/` — small heuristic explainers

A folder of single-purpose modules, each `(repo_path) → str | list | dict`:

| Module | Question it answers | How (roughly) |
|---|---|---|
| `project_summary.py` | what is this project? | README + top-level listing → Gemini; **heuristic fallback** (first README paragraph) when no key — the LLM is optional everywhere |
| `architecture.py` | how is it organized? | top-level folder scan |
| `entrypoints.py` | where does execution start? | finds `main.py`, `app.py`, `index.js`, `server.js`, skipping tests/docs |
| `folder_responsibilities.py` | what does each folder do? | name/content conventions |
| `reading_order.py` | what should I read first? | entry points → config → core services heuristic |
| `safe_contributions.py` | where can a newcomer safely start? | docs/tests/small leaf files |
| `feature_flow.py` | how does a request flow? | template narrative |
| `aggregator.py` | all of the above | calls each, returns one dict — the shape both the API and the OKF emitter consume |

(Several sibling modules — `code_smells.py`, `refactor_ideas.py`, `first_pr.py` etc. — are early drafts not wired into the aggregator; wedge material for Phase 3+.)

**Two bugs fixed here:** `project_summary.py` imported `ask_gemini`, which didn't exist anywhere (ImportError — now implemented as a sync helper in `llm/gemini.py`); and its old prompt sent only the repo *path* — the LLM can't read your disk, so it could only hallucinate. It now sends actual evidence (README + listing).

## `app/api/analysis.py`

- `GET /analysis/{repo_id}` → the aggregator dict. Fixed: the router **was never mounted** in `main.py` (unreachable feature), and it read `storage/repos/` which nothing populated — ingestion now persists a working copy to `REPOS_DIR`, which this reads.
- `POST /analysis/{repo_id}/okf` → regenerate the OKF bundle on demand.

## `app/services/okf_emitter.py` — the curated-knowledge bundle

**What OKF is:** Google's Open Knowledge Format — knowledge as a directory of Markdown files with YAML frontmatter (`okf`, `type`, `title`, `description`, `resource`, `tags`), links as plain markdown. No runtime, no SDK. Humans read it on GitHub; agents read it as files.

**What the emitter does:**
1. Runs the analysis aggregator over the persisted working copy.
2. Writes `data/okf/<repo>/.knowledge/`: `project-summary.md`, `architecture.md` (+ a knowledge-graph shape section: node/edge counts, low-confidence count), `entry-points.md`, `reading-order.md`, `folder-responsibilities.md`, `safe-contributions.md`, `feature-flow.md`, and a `README.md` index linking them all.
3. **Embeds each doc into the vector store under `space='okf'`** — the third retrieval source. This is why "why is it structured this way?" retrieves curated intent, not just code. Re-emission first removes the previous bundle's chunks (paths are stable), so it never duplicates.

**Design rule from the strategy (OKF is 17 days old, v0.1):** output-only, never load-bearing. Graph + vectors remain the source of truth; worst case we regenerate some Markdown. The emitter accordingly never raises — `okf.emit_failed` is logged and ingestion succeeds without it.

**Rebuild checkpoint:** after ingesting a repo, `ls backend/data/okf/<repo>/.knowledge/` shows 8 files; asking the chat "what is this project for?" should cite `.knowledge/project-summary.md` among its sources.
