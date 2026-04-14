# Documentation consolidation plan

## Goal
- Shorten/merge PACKAGING_GUIDE.md, installer_flow.md, TASKS_GUIDE.md, readme.md
- No repeated content; single source for each topic
- One place that lists all env variables and their use
- Execute in small chunks so nothing is left out

---

## Final structure

| File | Role | Content to include |
|------|------|--------------------|
| **README.md** | Entry point | Project overview, prerequisites, install/setup, quick start (run + one task example), message flow (one request/response), health & WebSocket URLs, **full env vars table**, CLI summary (table), links to TASKS_GUIDE and PACKAGING_GUIDE, packaging scaffolds (one line each), dev (reinstall, lint), contributing (short), troubleshooting (short). No deep task/queue or installer detail. |
| **TASKS_GUIDE.md** | Tasks & queues only | Overview (bullet list), quick start (tasks.py + .env minimal + run + one WS message), task registration (decorator, options table, sync/async, built-in ping), immediate vs queued (when to use, per-message override), queue config (queues.py example, auto-created), WebSocket protocol (connect URL, run-task, all response types compact), dequeue (WS + CLI), priorities/reorder (short), auto-update (flow + version-check messages), running (run + workers), CLI (table), one workflow example, tips (short), building executables (scaffold-windows/scaffold-macos one line each). Remove duplicate of readme content. |
| **PACKAGING_GUIDE.md** | Packaging & installers only | Quick start (scaffold → customize → build), what gets scaffolded (tree), **build flow** (from installer_flow), **install flow** (from installer_flow), hooks (pre_install/post_install, params table, one backup + one config example), customization (version, product name, custom entry point, workers.json), build commands (Windows/macOS + CLI), install locations (Windows/macOS), troubleshooting. Merge installer_flow.md into this and delete installer_flow.md. |
| **.env.example** | Reference | Keep all vars; ensure comments match the env table in README (use/purpose). |

---

## Content to remove or move (avoid duplication)

- **From readme:** Long message flow → keep one request/response example in README; full protocol in TASKS_GUIDE only. Task definition detail → TASKS_GUIDE. Packaging scaffolds → one line + link to PACKAGING_GUIDE.
- **From TASKS_GUIDE:** Duplicate install/setup (link to README). Duplicate env (link to README env table). Building executables → one line; installer builds → PACKAGING_GUIDE.
- **From PACKAGING_GUIDE:** Already fairly tight; add build/install flow from installer_flow.
- **From installer_flow:** Entire content merged into PACKAGING_GUIDE; then delete file.

---

## Env variables (single source: README + .env.example)

| Variable | Where used | Default | Purpose |
|----------|------------|---------|---------|
| **HOST** | cli.py | 127.0.0.1 | Agent server bind host |
| **PORT** | cli.py | 8080 | Agent server bind port |
| **WORKERS** | cli.py | 1 | Default number of workers (worker command) |
| **WORKERS_CONFIG** | cli.py | workers.json | Path to workers config file |
| **DB_FILENAME** | db.py, cli.py | eg_agent.db | SQLite DB filename (in app data dir) |
| **LOG_FILENAME** | log_config.py, cli.py | eg_agent.log | Log file name (in app data dir) |
| **DATABASE_URL** | db.py | sqlite:///&lt;db_path&gt; | Full DB URL (overrides DB_FILENAME location) |
| **AGENT_VERSION** | server.py | 1.0.0 | App version for auto-update version-check |
| **EG_AGENT_TASKS_DIR** | loader.py | — | Override directory to look for tasks.py |
| **EG_AGENT_PRELOAD_FILES** | loader.py | — | Comma/semicolon/space/newline list of files to preload before tasks.py |
| **EG_AGENT_PRELOAD_MODULES** | loader.py | — | Back-compat alias for EG_AGENT_PRELOAD_FILES |
| **EG_AGENT_TRAY_ICON** | tray.py | — | Path to tray/menu bar icon (e.g. icon.icns, icon.png) |
| **AGENT_SERVER_URL** | queue.py | http://localhost:8080 | Agent server URL used by workers/queue code |
| **EG_AGENT_BUILD_ENTRY** | build_*_installer.py | — | Custom entry script for installer build |
| **EG_AGENT_BUILD_VERSION** | build_*_installer.py | 1.0.0 | Installer/product version |
| **EG_AGENT_PKG_NAME** | build_macos_installer.py | EG-Agent.pkg | macOS PKG output filename |
| **EG_AGENT_APP_NAME** | build_macos/ windows | eg-agent | App name (folder, executables, .app) |
| **EG_AGENT_CREATE_DESKTOP_SHORTCUT** | build_macos_installer.py | true | Create desktop shortcut on macOS install |
| **CODESIGN_IDENTITY** | build_macos_installer.py | — | Code signing identity for macOS build |
| **LOCALAPPDATA** | loader, installer_helper | — | Windows: data root (optional override) |

---

## Execution chunks

1. Create this plan (PLAN_DOCS.md).
2. Rewrite README.md (overview, quick start, env table, CLI table, links, contributing, troubleshooting).
3. Rewrite TASKS_GUIDE.md (shortened, no repeat, reference README for env/install).
4. Merge installer_flow into PACKAGING_GUIDE.md (add flow sections, remove duplicate text).
5. Update .env.example comments to align with env table.
6. Delete installer_flow.md.
