# DepGuard AI

**The Autonomous Dependency Architect** — an AI-powered tool that scans your project for outdated or vulnerable dependencies, analyzes breaking changes across versions, generates safe migration patches using LLMs, and verifies the result automatically.

---

## Overview

DepGuard AI combines static code analysis, multi-provider LLM reasoning, and iterative verification to automate the entire dependency upgrade lifecycle:

```
Scan → Analyze → Scout → Patch → Check → Repair
```

1. **Scan** — Discover all dependencies across every ecosystem in your project
2. **Analyze** — Query OSV and deps.dev for CVEs, severity, and latest versions
3. **Scout** — Use an LLM to read changelogs and identify breaking API changes
4. **Patch** — Generate multi-file code patches via AST analysis + LLM reasoning
5. **Check** — Run your project's own build and test commands to verify correctness
6. **Repair** — If checks fail, a Repair Agent reads the errors and fixes the code

All of this is surfaced through a live-streaming IDE-like UI where you can inspect every diff hunk, accept or reject individual changes, and roll back if needed.

---

## Features

- **Multi-ecosystem support** — `requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `pom.xml`, and more
- **30+ language AST parser** — Tree-sitter powered analysis of Python, JS/TS, Go, Rust, Java, and others
- **Real-time streaming UI** — Live terminal-style progress log while the pipeline runs
- **Hunk-level review** — Accept or reject individual diff hunks before anything is written to disk
- **LLM provider fallback** — Claude → Gemini → Qwen with automatic failover
- **Impact graph** — Visual call-graph showing which code paths are affected by each dependency
- **Undo support** — Git checkpoint created before every apply; one-click rollback
- **Verification loop** — ProjectChecker + RepairAgent retry cycle until the build is clean

---

## Architecture

![DepGuard AI Architecture](frontend/src/assets/architecture.svg)

### Agents

| Agent | Role |
|---|---|
| `ScannerAgent` | Parses dependency manifests across all ecosystems |
| `WatchdogAgent` | Queries OSV for CVEs, deps.dev for latest versions, classifies severity |
| `ScoutAgent` | Reads changelogs and release notes via LLM to identify breaking changes |
| `PatchAgent` | Generates code patches using AST context + LLM; streams per-file progress |
| `ProjectChecker` | Runs the project's own build/test commands to verify patches |
| `RepairAgent` | Reads compiler/test errors and applies targeted fixes in a retry loop |

### Tools

| Tool | Role |
|---|---|
| `llm_router.py` | Multi-provider LLM abstraction with caching and fallback |
| `ast_scanner.py` | Tree-sitter AST scanner for API usage detection across 30+ languages |
| `impact_graph.py` | Call-graph builder to compute the impact radius of a dependency change |
| `lockfile_resolver.py` | Resolves transitive dependency versions from lock files |

---

## Getting Started

### Prerequisites

- Python 3.11+
- Node.js 18+
- Git (used for checkpoint commits)
- At least one LLM API key (Claude recommended)

### 1. Clone

```bash
git clone https://github.com/your-org/depguard-ai.git
cd depguard-ai
```

### 2. Backend

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Frontend

```bash
cd frontend
npm install
```

### 4. Environment

Create a `.env` file at the project root:

```env
# LLM Providers (at least one required)
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...

# GitHub token for changelog fetching (optional but recommended)
GITHUB_TOKEN=ghp_...

# Provider selection
ENABLE_CLAUDE=true
ENABLE_GEMINI=false
ENABLE_QWEN=false
LLM_PROVIDER_ORDER=claude,gemini,qwen

# Optional tuning
SCOUT_LLM_MAX_TOKENS=8000
PATCH_TARGET_MAX_LINES=400
DEPGUARD_AUTO_REPAIR=true
DEPGUARD_REPAIR_MAX_ATTEMPTS=1
```

### 5. Run

```bash
# Terminal 1 — backend (port 8000)
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2 — frontend (port 5173)
cd frontend
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

---

## Usage

### Dashboard

1. Enter your project path in **Target Project Directory** and click **Scan Project**
2. DepGuard discovers all dependencies, checks for CVEs, and displays a health score
3. The **Activity Log** streams every scan phase in real time
4. Click **Open IDE Workspace** to review and apply updates

### IDE Workspace

1. Select a package in the **Right Panel → Dependencies** and click **Update**
2. The **Progress** tab streams the pipeline live: AST scan → Scout analysis → Patch generation
3. Green/red diff hunks appear in the middle editor panel
4. Use **Accept** / **Reject** on individual hunks or entire files
5. Click **Apply Accepted** — DepGuard writes the files, runs the checker, and repairs any failures
6. An **Undo** button appears after apply; clicking it rolls back via the git checkpoint

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/providers` | LLM provider statuses |
| `GET` | `/scan-stream` | Stream dependency scan results (SSE) |
| `POST` | `/preview-stream` | Stream patch preview generation (SSE) |
| `POST` | `/apply` | Write accepted patches; run checker + repair |
| `DELETE` | `/preview/{session_id}` | Discard a pending preview session |
| `POST` | `/rollback` | Roll back to a pre-apply git checkpoint |
| `POST` | `/impact-graph` | Build or retrieve the code impact graph |
| `GET` | `/files` | List all project files |
| `POST` | `/file-content` | Read a specific file's content |
| `GET` | `/browse` | Open native directory picker |

---

## Project Structure

```
depguard-ai/
├── agents/
│   ├── scanner.py       # Dependency discovery
│   ├── watchdog.py      # CVE & version analysis
│   ├── scout.py         # Breaking change detection
│   ├── patch.py         # LLM-powered patch generation
│   ├── checker.py       # Build/test verification
│   └── repair.py        # Error recovery
├── tools/
│   ├── llm_router.py    # Multi-provider LLM abstraction
│   ├── ast_scanner.py   # Tree-sitter code analysis
│   ├── impact_graph.py  # Call graph & impact radius
│   └── lockfile_resolver.py
├── api/
│   └── main.py          # FastAPI application
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── components/
│       │   ├── DashboardView.tsx
│       │   ├── IdeWorkspaceView.tsx
│       │   ├── DiffReviewPanel.tsx
│       │   ├── PackagesTable.tsx
│       │   ├── ProjectDependencyGraph.tsx
│       │   └── HealthScore.tsx
│       └── hooks/
│           └── useDepGuard.ts   # All API calls
├── tests/
├── requirements.txt
└── .env
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (for Qwen) |
| `GITHUB_TOKEN` | — | GitHub PAT for changelog fetching |
| `ENABLE_CLAUDE` | `true` | Enable Claude provider |
| `ENABLE_GEMINI` | `false` | Enable Gemini provider |
| `ENABLE_QWEN` | `false` | Enable Qwen via OpenRouter |
| `LLM_PROVIDER_ORDER` | `claude,gemini,qwen` | Fallback order |
| `LLM_CACHE_ENABLED` | `false` | Cache LLM responses to disk |
| `SCOUT_LLM_MAX_TOKENS` | `8000` | Max tokens for Scout analysis |
| `PATCH_TARGET_MAX_LINES` | `400` | Max lines per file sent to Patch LLM |
| `DEPGUARD_AUTO_REPAIR` | `true` | Run Repair Agent after failed check |
| `DEPGUARD_REPAIR_MAX_ATTEMPTS` | `1` | Max repair retry iterations |

---

## Tech Stack

**Backend**: Python 3.11 · FastAPI · uvicorn · httpx · tree-sitter · anthropic · google-genai · langgraph

**Frontend**: React 18 · TypeScript · Vite · TailwindCSS 4 · shadcn/ui · @xyflow/react · lucide-react

---

## License

MIT
