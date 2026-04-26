# DepGuard AI: Implementation Plan & Next Steps

Based on your `idea.pdf` and the vision for an Autonomous Dependency Architect, you are building a highly sophisticated Multi-Agent System that combines **Static Analysis (AST)** with **LLM-based Code Refactoring**. 

Since we have initialized the basic backend directory structure (FastAPI, `agents/`, `tools/`, etc.), here is a proposed, phased implementation plan to build out the core system sequentially.

## User Review Required

> [!IMPORTANT]
> Please review this phased roadmap. Let me know which phase you would like to tackle first! I recommend starting with **Phase 1**, as it lays the foundation for everything else.

## Open Questions

> [!TIP]
> 1.  **LLM Provider**: Do you prefer to use OpenAI (GPT-4o) or Anthropic (Claude 3.5 Sonnet) for the `Patch Agent`, or should we use the Gemini model currently configured in this environment?
> 2.  **Package Manager Focus**: Should we strictly focus on Python (`requirements.txt`, PyPI) first to get an MVP working, before expanding to Node.js (`package.json`, npm)?
> 3.  **Version Resolution**: For the `z3_solver.py`, do you want to build a rigorous version constraint SAT solver from scratch, or rely on a simpler topological sort (as mentioned in the PDF via `networkx`) initially?

---

## Proposed Roadmap

### Phase 1: Core Scanning & Registry Polling (MVP Foundation)
We need the system to understand what dependencies are currently installed and what new versions exist.
- **`agents/scanner_agent.py`**: Reads `requirements.txt` and extracts the list of dependencies and their current versions.
- **`tools/registry_api.py` [NEW]**: A utility to poll the PyPI JSON API to check for the latest versions and fetch changelog URLs.
- **`tools/vulnerability_scanner.py` [NEW]**: Integrates with the OSV.dev API or GitHub Advisory Database to check for CVEs associated with the current package versions.

### Phase 2: Static Analysis (The "Smart" part of the Scanner)
Before updating a version, we need to know how the package is used in the codebase.
- **`tools/ast_scanner.py`**: Implement the `DeprecatedAPIScanner` using Python's `ast` module. We will build a visitor that scans the project codebase to find usages of specific APIs (e.g., tracking `import pandas as pd`).
- **Dependency Graph**: Implement the `nx.DiGraph` logic from your PDF to compute the safe update order using topological sort.

### Phase 3: The LLM Patch Agent (The "Autonomous" part)
This is where the magic happens. Once we know a new version breaks an old API, the LLM writes the patch.
- **`agents/patch_agent.py`**: 
  - Retrieves changelogs for the package update.
  - Takes the specific code snippets found by `ast_scanner.py`.
  - Prompts the LLM (e.g., "You are an expert on this package migration. Here is the old code, the new API from the changelog is X. Generate a JSON code patch.").
- **`tools/patch_applier.py` [NEW]**: Safely applies the LLM-generated patch back to the source code files.

### Phase 4: Runner & Validation (Feedback Loop)
We must ensure the LLM's patch actually works.
- **`agents/runner_agent.py`**: Automatically runs `pytest` after applying the patch and updating the dependency version.
- **Rollback System**: If tests fail, the system rolls back the changes using `git` checkpoints.

### Phase 5: API Layer & Database
Expose the capabilities via FastAPI so a frontend (or CLI) can interact with it.
- **`api/routes.py`**: Endpoints like `POST /scan`, `GET /status`, `POST /apply-patch`.
- **`database/models.py`**: Store Project data, Health Scores, and Update History (using SQLite/PostgreSQL).

## Verification Plan

### Automated Tests
- Create unit tests for `ast_scanner.py` using dummy Python code files with known deprecated APIs to ensure the AST visitor catches them.
- Create mock tests for the `Patch Agent` to verify that the LLM generates valid JSON patch formats.

### Manual Verification
- We will set up a dummy repository with outdated dependencies (e.g., an old version of `numpy` or `pydantic`).
- Run the DepGuard CLI/API locally to see if it correctly detects the outdated dependency, finds the deprecated API usage via AST, uses the LLM to rewrite the code, updates the requirements, and successfully runs tests.
