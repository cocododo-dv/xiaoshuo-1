# CLAUDE.md

Durable contracts, commands, guards and gotchas only. Dated narratives (snowflake 阶段 A–Z, the 2026-09 rounds) are
archived verbatim in `docs/history/claude-md-log-2026-09.md`; module docstrings and `docs/` carry the detail. When this
file and the code disagree, the code wins and this file gets fixed (`tests/test_docs_links.py` checks its cited paths
and caps it at 32 KB).

## Product scope

Single-machine, single-author long-form novel writing system (Chinese-language product). `README.md` is the
product-truth entry, `docs/README.md` indexes every doc, `docs/operator-manual.md` documents the React workbench only.
Mainline: create work → 10-step snowflake ideation → materialize chapters / scenes → per-scene AI drafting or manual
writing → author review promotes authoritative text → final approval in 成稿中心. Not a multi-user SaaS, HA service or
publication arbiter.

- **Retired, do not re-introduce**: the 2026-09 subtraction (Vue frontend, outcome governance, knowledge / vector
  consoles, interop center, 长篇控制塔, advisory narrative overlays …; full list in the history log) and the 2026-09-29
  refactor (Chroma, global LLM quota fences, `task_routing`, voice / relation cards, the v1 snowflake planner and its
  routes, author-preference learning). Retired routes: `tests/test_retired_surface.py`.
- Kept only because tests build on them (React never calls them): `POST/GET /api/v1/projects`,
  `POST/GET /api/v1/chapters`, `POST /api/v1/scenes`, `GET /api/v1/projects/{id}/dashboard`, the synchronous
  `POST /api/v1/projects/{pid}/chapters/{cid}/run` and `POST /api/v1/chapters/{id}/run/full`, and
  `POST /api/v1/scenes/{id}/run/full`. New tests seed rows with `tests/support/seed.py` instead of the create routes.
- **No demo data, no fake generation.** Every generation node is fail-closed: no live LLM → 409/502 with an
  `author_action`, never canned prose. Neutral fixtures exist only for tests and the E2E lane:
  `backend/tests/fixture_works.py` (`seed_fixture_works`, works `work-a` / `work-b` — keep these literal ids) and
  `backend/tests/fixture_runtime.py` (`seed_runtime_fixture`, also `PRJ_DEMO_CH001`); the product never seeds them.
- Operator tools, run from `backend/` as `python -m novel_system.tools.<name>`: `reset_author_state`, `db_backup`,
  `compact_db`, `sync_prompt_templates`, `raise_llm_output_budget`, `refresh_style_reference_books`,
  `purge_style_reference_books`, `build_voice_baseline`. Database writers dry-run unless `--execute`; those that open
  the app's session also refuse `--execute` (exit 2) when the DB revision differs from the code (`tools/_cli.py`).
  Every tool refuses to run another checkout's code (exit 2, `tools/_checkout_guard.py`; Alembic's `env.py` too).

## Commands

### Full stack
- Linux (the dev host): `scripts/start-all-linux.sh` / `scripts/stop-all-linux.sh` (logs and pids in `.codex-run/`;
  waits for `/ready`, fails fast with the log tail). Per leg, each stopping its previous instance:
  `scripts/start-backend-linux.sh` (prunes old `.codex-run` logs, exports `PYTHONPATH=backend/src`, `alembic upgrade
  head` with `backend/.venv/bin/python`, uvicorn `--reload`) and `scripts/start-frontend-linux.sh` (Node from
  `NOVEL_SYSTEM_NODE_BIN` → nvm → `~/.local/node/bin` → `PATH`; refuses a missing node / `node_modules` or a Node below
  the `engines` floor ≥ 18.18 before stopping anything — use Node 22).
- Windows: `.\start-dev.cmd` / `.\stop-dev.cmd` / `.\restart-dev.cmd` (→ `scripts/dev.ps1`: migrate, start both, open
  the browser; a busy port scans upward and the chosen URLs go to `.codex-run/backend.url` / `frontend-react.url`);
  `.\reset-runtime-keep-llm.cmd` resets the runtime DB / artifacts but keeps the LLM config.
- Addresses: React `http://127.0.0.1:5174`, backend `http://127.0.0.1:8000`. `GET /live` = process only; `GET /ready`
  = DB + migration revision + required tables / columns.
- `NOVEL_SYSTEM_CONFIG_SECRET` is generated once into `.codex-run/config.secret` (0600); lose it and every provider key
  saved through the UI becomes undecryptable.

### Backend (Python 3.12 / FastAPI, `backend/`)
- Interpreter = the uv-locked venv, never a system Python: `cd backend && uv sync --locked --extra dev` →
  `backend/.venv/bin/python` (Windows `backend\.venv\Scripts\python.exe`).
- Run pytest / alembic **from `backend/`** (`pythonpath=["src"]`, `--strict-markers --strict-config`):
  `.venv/bin/python -m pytest tests/<file>.py`, `-k name`; one CI shard: `.venv/bin/python scripts/pytest_shard.py
  --shard-index 0 --shard-count 4 -- -q`. ≈3,700 tests, about half an hour on the 2-CPU dev host — run it in the
  background. Concurrent pytest sessions need distinct `--basetemp`.
- Shards are greedy LPT over `tests/.durations.json`; refresh it after adding or splitting test files
  (`scripts/pytest_shard.py --update-durations <junit xml of every shard>`).
- Ruff is correctness-only (`ruff check src tests`: E9 / F63 / F7 / F82 / F401 / F841; an import kept for a monkeypatch
  carries `# noqa: F401` with the reason). No formatter: never reformat code you did not change.
- Dependencies: CI installs `requirements.lock` with `--require-hashes`; `uv.lock` is canonical. After editing
  `pyproject.toml`: `uv lock --python 3.12`, then `uv export --locked --extra dev --no-emit-project --format
  requirements-txt --output-file requirements.lock` (dev extra only, never `--all-extras`) and review both files.
  `tests/test_dependency_lock.py` pins this. Only `frontend-react/` has a Node lockfile.
- Windows lanes: `scripts/verify_windows.ps1` (ruff, pip-audit, 4 pytest shards, vitest, build) and
  `scripts/verify_release.ps1` (that plus the React contract E2E).

### Frontend (`frontend-react/`, Vite + React 18)
`npm ci` · `npm run dev` (:5174) · `npm run lint` (ESLint 9 flat config: `react-hooks/rules-of-hooks` is an error,
`exhaustive-deps` a warning, stale disable comments are errors) · `npm test` (vitest, ≈100 files / ≈1,700 tests,
about ten minutes single-worker here) · `npm run build`. `engines.node` is `>=18.18`; CI uses Node 22. Build-time API
base `VITE_NOVEL_SYSTEM_API_BASE` (default `http://127.0.0.1:8000`).

### Contract E2E
`NOVEL_SYSTEM_PYTHON=$PWD/backend/.venv/bin/python bash scripts/verify_react_e2e.sh` (Windows
`scripts/verify_react_e2e.ps1`): fresh migrated sqlite under `.codex-run/e2e-linux/`, seeded backend `:8009`, React dev
server `:5176`, real Chromium, then `frontend-react/scripts/run-smokes.mjs` (acceptance, `smoke-phase2..7`,
`smoke-ai-settings`, `qa2-ui`; fixtures reseeded before each suite). Ports via `PLAYWRIGHT_BACKEND_PORT` /
`PLAYWRIGHT_REACT_PORT`; the lane refuses (exit 2) a port that already has a listener. `npx playwright install
chromium` once. The smokes share `frontend-react/scripts/lib/harness.mjs` (`openApp`, `waitUntil`, `reseedFixtures`;
aborts `:8000`) and need `npm run dev`: they reach stores through the DEV-only `window.__wsStores`.

### CI (`.github/workflows/ci.yml`; PRs and pushes to main / master / codex/**)
Backend Quality Gates (`pip check`, ruff, hashed `pip-audit`, `test_service_architecture.py` +
`test_dependency_lock.py`) · Backend Tests (4 LPT shards, JUnit artifacts) · Frontend Tests (`npm ci` → `npm audit
--omit=dev --audit-level=high` → `npm run lint` → vitest → build) · React Contract E2E. Backend jobs set
`PYTHONPATH: src`.

### Migrations, backup, deploy
`cd backend && .venv/bin/python -m alembic current | heads | upgrade head`. Full rules, the per-revision table, the
irreversible revisions and the refactor deploy order: `docs/migrations.md`. The short version:
- **Adding a migration = bump `db/schema_contract.py:CURRENT_SCHEMA_REVISION`** (`test_schema_contract_revision.py`
  pins it to the single head); historical revisions are frozen explicit DDL and never import the ORM; a migration test
  upgrades to its own revision (`tests/support/migrations.py`).
- A run that applies revisions fails when it leaves new foreign-key violations (pre-existing ones only warn).
- Back up **with the services stopped** (`db_backup --backup SRC DST`, `--verify`, `--restore`; drills
  `scripts/db_backup_drill.{sh,ps1}`); never plain-copy the WAL-mode `novel_system.db`.
- Deploy: stop → `db_backup` (kept as a named archive) → fast-forward → `alembic upgrade head` → `compact_db PATH
  --execute` → `sync_prompt_templates --execute` (installs with a prompts snapshot) → start → `/ready`.

### Author-state reset
`python -m novel_system.tools.reset_author_state` (dry-run) / `--execute --yes`: wipes project / snowflake / chapter /
run data, keeps reference books, profiles and system config. A new table must be a reset target or listed in
`PRESERVED_TABLES` (completeness guard in `tests/test_reset_author_state.py`).

## Drift guards (they fail the suite — know them before adding code)
Backend (`backend/tests/`):
- `test_metadata_isolation.py` — the schema from `create_all` (tests) and from Alembic (dev / prod; the app never
  builds it) must match per table / column / named index; migration-only indexes go in the model's `__table_args__`.
- `test_service_architecture.py` — no import cycles; `services/` and `api/` never import `tools/`; ownership comes from
  relation columns, never parsed ids; only `style_policy` resolves binding state; each `SHARED_HELPER_LEAVES` entry
  imports only what it lists. `test_models_package.py` — every mapped class lives in a `db/models/<domain>.py` submodule
  and is exported by the facade.
- `test_route_mutation_policy.py` — every write handler goes through `api/mutations.mutate()`, owns no transaction,
  hand-copies no method / path, and no router is mounted with a prefix; `test_api_request_models.py` (bodies in
  `api/requests/`, `extra="forbid"`); `test_idempotency_contract.py`; `test_api_openapi_contract.py` (envelopes on every
  versioned operation; operation floors just under the live counts — lower them when retiring a route);
  `test_routes_all_manifest.py`; `test_retired_surface.py`; `test_fixture_import_boundary.py`.
- `test_error_catalog.py` — a `DomainError` code with an English message needs a Chinese entry in
  `api/error_catalog.ERROR_MESSAGES`.
- `test_prompt_template_contracts.py` — every `config/prompts.yaml` template has `version` (`YYYY-MM-DD.vN`),
  `input_token_budget`, prompts and `structured_schema`; array members declare `properties`; scores declare
  `minimum` / `maximum`. Bump `version` on every prompt edit; never pin a version string in a test.
- Inventories a new table may have to join: `test_scene_rehome.py` (`scene_rehome.REHOMED_MODELS` / `NOT_REHOMED_TABLES`),
  `test_trash_purge_completeness.py` (`services/project_purge.py`), `test_reset_author_state.py`
  (`PRESERVED_TABLES`); new caches: `test_cache_registry.py`.
- `test_schema_contract_revision.py`, `test_dependency_lock.py`, `test_suite_layout.py`, `test_retired_demo_guard.py`,
  `test_docs_links.py` (every tracked `.md` link resolves; this file's cited paths exist; this file ≤ 32 KB).
- Goldens — regenerate deliberately and say why in the commit: `tests/golden/style_reference/expected/*.json`
  (`regen_expected.py`, when sentence splitting, the heuristic classifier or the measurement kernel changes; bump
  `measure.KERNEL_VERSION` and rebuild `voice_baseline.yaml` with `build_voice_baseline build-baseline` for the kernel),
  `BUNDLE_GOLDEN_REGEN=1`, `LITERARY_GOLDEN_REGEN=1`, `NARRATIVE_GOLDEN_REGEN=1`, `CHECKPOINT_FORMAT_GOLDEN_REGEN=1`
  (checkpoint snapshot keys are persisted: changing one makes stopped runs read as corrupt).

Frontend (fail `npm test`): `tooling-independence.test.js` (Playwright pinned, no static ESM cycles, only
`ws-test-seam.js` writes `window` and only in DEV, icons `I` imported from `icons.jsx`); `design-guard.test.js`
(tokens defined, keyframes once, each stylesheet imported once by `main.jsx`, no unreferenced classes, no raw colours,
`--tone` only via `[data-tone]` / `:where()`, `--fs-*` font sizes ≥ 11px; its `KNOWN_*` / `MAX_*` ratchets only shrink,
`MAX_LEGACY_PILL_CLASS_USES` is 0 — use `<Tag>`); `build-chunking.test.js` (views are `lazyNamed` routes, the
`import("./ws-*.jsx")` literals stay in `ws-app.jsx`, only `vendor` is split in `frontend-react/build-chunks.js`);
`frontend-boundary-contract.test.js` (replace-only alias redirects, self-hosted fonts, CSP meta, `build-csp.js`);
`runtime-truth-contract.test.js`.

## Test conventions
- `tests/conftest.py`: each test gets a copy of a session-built `create_all` template DB; the shell's `NOVEL_SYSTEM_*`
  are scrubbed (passthrough: `NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS`, `NOVEL_SYSTEM_PYTHON`); `novel_system` may come only
  from this checkout's `backend/src`; registered caches reset around each test; style-job sweepers must stop with the
  test; `NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER=5` (the disarmed default is covered in
  `test_scene_token_budget.py`). pytest refuses to open the repository's `backend/novel_system.db`.
- `client` is `tests/support/api_client.AutoKeyTestClient` (adds a fresh `X-Idempotency-Key` to writes without one);
  `raw_client` sends none (for the 400 cases). Opt-in named fixtures (`tests/support/fixtures.py`, loaded via
  `pytest_plugins`): `online_pipeline`, `skeleton_snowflake`, `skeleton_snowflake_llm_on`, `online_author_pipeline`,
  `online_orchestrator_runner`, `style_workers` — use `pytestmark = pytest.mark.usefixtures("…")`.
- Shared helpers live in `tests/support/` (`seed.py`, `migrations.py`, `sql.count_statements`, `llm_fakes.py` incl.
  `build_fake_paragraph_classifier` / fixture `fake_paragraph_classifier`, …); a test module never imports another
  test module, and each support module is registered for assert rewriting in `tests/support/__init__.py`
  (`test_suite_layout.py`). Declare new markers in `pyproject.toml` (`--strict-markers`).
- Fake LLM clients inherit `tests/accounted_llm_fakes.AccountedGenerateMixin` (a bare `generate` bypasses the ledger).
  Patch LLM settings / routing / template loaders at `novel_system.services.llm_service_base.<name>`. A new
  process-level cache keyed on DB content registers `cache_registry.register_cache_reset` at its definition and is
  added to `test_cache_registry.KNOWN_CACHES`.
- Style reference: `tests/style_reference_factories.py`, `tests/style_reference_route_helpers.import_book`; a real book
  only via `NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS` (opt-in `test_style_reference_local_corpus.py`, never committed).
- Frontend: vitest projects `dom` (jsdom, default, `src/test-setup.js`, 15 s timeout) and `node` (the `NODE_TESTS` list
  in `vitest.config.js`: pure logic and source-reading guards). Store tests `vi.mock("./lib/client.js")`, route `apiGet`
  through `src/test-helpers.js` `installApiRouter`, settle with `settleActiveWork` / `settleCatalog`, and load modules
  in isolation (`vi.resetModules()` + dynamic import). Keep tests falsifiable — check the rollback / alert path trips.
- **Public repo**: synthetic world only (林昭 / 雨城 / 旧信 / 案卷 …, works `work-a` / `work-b`); never the author's
  manuscript names, places or plot text. Scan new test files before committing.

## Environment variables (read by the application)
| Variable | Default | Notes |
|---|---|---|
| `NOVEL_SYSTEM_DATABASE_URL` | `sqlite:///<backend>/novel_system.db` | |
| `NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED` | `true` | |
| `NOVEL_SYSTEM_LLM_ENABLED` / `…_LLM_PROVIDER` / `…_LLM_BASE_URL` / `…_LLM_API_KEY` | `false` / `openai_compatible` / OpenAI / — | provider = adapter key in `services/llm_providers/`; the active 系统配置 api snapshot overrides them |
| `NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS` | `900` | `0` = no response ceiling; connect timeout stays finite; route `timeout_seconds` → api snapshot → env; probes always finite |
| `NOVEL_SYSTEM_CONFIG_SECRET` | launcher-generated | encrypts saved provider keys; without it → 403 `CONFIG_SECRET_REQUIRED` |
| `NOVEL_SYSTEM_ADMIN_TOKEN` | — | admin endpoints (`X-Admin-Token`); unset = loopback clients only |
| `NOVEL_SYSTEM_LOCAL_ONLY` / `…_REMOTE_ACCESS_TOKEN` | `true` / — | `false` requires the token (`X-Novel-Access-Token`); a shared gate, not auth |
| `NOVEL_SYSTEM_CORS_ORIGINS` / `…_CORS_ALLOW_CREDENTIALS` | :5173 / :5174 / :5175 / :8081 / `true` | |
| `NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL` | `false` | adds `error.details.debug_message` (the replaced English text, or a 500's exception text) |
| `NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES` | 16 MiB | |
| `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS` | `3600` | startup reclaim of orphaned non-scene reservations |
| `NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER` | `0` (disarmed) | `N` arms `N × baseline` for newly initialized scenes — the only fence left |
| `NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET` / `…_SCENE_INPUT_TOKEN_BUDGET` | `0` | `0` = template budget (scene family: max with `RUNTIME_MIN_INPUT_BUDGETS`); positive = override for small-context models |
| `NOVEL_SYSTEM_SCENE_STRUCTURE_BRIEF` / `…_SCENE_DESIGN_CONTEXT` | `true` | rollback switches only |
| `NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED` | `false` | style-first bindings only (first draft + targeted revisions ranked by fidelity, critical scenes pause at the blinded author selection); every other work drafts one candidate |
| `NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED` / `…_LLM_EVENT_EXTRACTION_ENABLED` | `false` | opt-in editor critic / archive-time prose event extraction |
| `NOVEL_SYSTEM_CONTENT_SAFETY_MODE` | `review` | or `audit` |
| `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS` | — | server-path book import disabled until set |
| `NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON` | — | extra protected terms for the copy gate (warn only) |

Retired and ignored except for one startup warning: the six `NOVEL_SYSTEM_LLM_*_LIMIT` / `…_MAX_CONCURRENT_REQUESTS`
quota vars and `NOVEL_SYSTEM_LLM_{INPUT,OUTPUT}_COST_PER_MILLION_USD` (`env_config.RETIRED_ENV_VARS`). Launcher / lane /
test only: `NOVEL_SYSTEM_BACKEND_PORT` (8000), `…_FRONTEND_PORT` (5174), `…_NODE_BIN`, `…_ARTIFACT_RETENTION_DAYS`
(14), `NOVEL_SYSTEM_PYTHON`, `PLAYWRIGHT_*_PORT`, `STYLE_REFERENCE_REPO_ROOT` (migration 0036 test override),
`NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS`, `VITE_NOVEL_SYSTEM_API_BASE`, `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`.

## Architecture

### Backend (`backend/src/novel_system/`)
- `api/app.py` mounts one router per area (`api/routes/`); bodies in `api/requests/`; envelope
  `{ok, data, error:{code,message,details}, request_id}` via `api/response.respond` / `error`; writes via
  `api/mutations.mutate()`; Chinese error text via `api/error_catalog.py`; `api/middleware.py` wraps unhandled
  exceptions as `INTERNAL_ERROR` inside CORS and answers 503 `SERVICE_NOT_READY` on `/api/*` while the schema is behind.
  Every response carries `X-Request-Id`; `X-Operator-Ref` is the audit actor (local mode only).
- `db/models/` is a package of domain submodules behind the `db.models` facade; `env_config.py` is the only settings
  module (`settings.get_settings()` = env + the active api snapshot).
- A missing prerequisite answers with an `author_action` (`services/author_actions.py`); LLM failures become domain
  errors only through `services/llm_fail_closed.py` (409 capability / 502 upstream).
- Big services are packages or facades over mixins; the facade docstring lists the parts and the monkeypatch seams:

| Area | Entry points (`services/`) | Contract doc |
|---|---|---|
| Snowflake | `snowflake_workspace.py` (facade over seven mixins), `snowflake_steps.py` (facade), `snowflake_workspace_llm.py` + `snowflake_llm_*`, `snowflake_staleness.py`, `snowflake_direction_brief.py` | `docs/snowflake-method-contract.md`, `docs/snowflake-coach-and-directions.md` |
| Chaptering / catalog | `snowflake_chaptering/`, `snowflake_chapter_table.py`, `materialization.py`, `catalog.py`, `projects.py` (facade), `chapter_final_flow.py`, `scene_rehome.py` | `docs/book-spine-catalog-contract.md` |
| Scene run | `orchestrator.py` (facade over `scene_run/`), `scene_generation/`, `bundle_builder.py` + `bundle_*.py`, `qc_engine/`, `near_final.py`, `scene_run_jobs.py`, `chapter_runner.py`, `background_recovery.py`, `archive_effects_plan.py` | — |
| Diagnosis / quality | `scene_diagnosis/`, `literary_quality/` (20 rule dimensions; `calibration_source.py`), `writer_deep_review.py` + `writer_deep_review_{output,prompts,patches}.py` | `docs/scene-diagnosis-2026-09-22.md` |
| Style reference | `style_reference/` (its `__init__` exports nothing — import submodules), `style_policy.py`, `style_prompt_injection.py`, `style_fidelity_view.py`, `reference_copy_gate.py` | `docs/style-reference.md` |
| Final text / canon | `canonical_manuscripts.py`, `final_text_gate.py`, `chapter_approval.py`, `canon_continuity.py` (facade over `canon/`), `narrative_event_log.py` (facade over `narrative/`), `aggregator.py` | `docs/canon-continuity.md` |
| LLM | `llm_client.py`, `llm_providers/` (provider behaviour lives here), `llm_routing.py`, `llm_node_registry.py`, `llm_accounting.py` (+ `llm_ledger_*`, `llm_scene_fence.py`), `llm_service_base.py`, `structured_llm_call.py`, `system_config.py`, `cost_aggregation.py` + `pricing.py` | `docs/runtime-safety.md` |
| Jobs | `background_jobs.py`, `run_job_leases.py`, `maintenance.py` (periodic tasks, `run_at_start=False` waits a full interval) | `docs/runtime-safety.md` |

- Shared helper homes — import, never copy: `env_parsing.py`, `cache_registry.py`, `services/value_coercion.py`,
  `services/hash_engine.py` (`sha256_text`, `sha256_json_plain`, `sha256_json_normalized`, `json_plain`),
  `services/scene_lookup.py` (`require_scene`, `require_chapter`, `require_project` …), `services/scene_text.py`,
  `services/planning_queries.py`, `services/snowflake_queries.py`, `services/text_input.py`, `services/story_slots.py`,
  `services/catalog_labels.py`, `services/catalog_ordering.py` (park-then-place), `services/pagination.py`
  (`page`/`page_size` or `cursor`/`limit`), `api/deps.py`.
- Config lives in the root `config/`: `models.yaml` (only `retry_budget` / `job_runtime`; node defaults live in
  `llm_node_registry.py`), `prompts.yaml`, `pricing.yaml` (author-entered prices; unpriced models show 「未定价」),
  `allowlists.yaml`, `hash_contract.yaml`, `style_reference/*`.

### Frontend (`frontend-react/src/`)
- Stores are plain ES modules — import them (`WsWorks` `ws-works.jsx`, `WsCatalog` `ws-catalog.jsx`, `SnowSync`
  `ws-snow-sync.jsx`, `WrDocs` `wr-doc-store.jsx` over `wr-doc-sync.js`, `WsManuStore`, `WsDiagnosis`, `WsAuthorAi`,
  `ws-review-store.js`, `ws-library-store.js`, `ws-styleref-store.js` …): sync caches, optimistic write + rollback /
  refetch, change notifications via `subscribe(fn)`; each module header documents its store's contract.
  `ws-test-seam.js` is the only `window` writer (`window.__wsStores.load(...)`, DEV only, for the smokes).
- Shell: `ws-nav.js` is the pure navigation model (`WS_NAV_GROUPS`, `WS_VIEW_ALIAS`: `deepdesk → writer`,
  `flowmap → home`); `ws-app.jsx` keeps routing, the lazy imports and `<ViewReady>`; cross-view intents go through
  `ws-view-intents.js` (mount handshake). Megafiles are split behind entry modules that re-export what tests import.
- Shared layer — use it instead of per-view look-alikes: tokens in `styles.css`; `ws-ui.jsx` + `ws-ui.css`
  (`PageHeader`, `Segmented`, `Tabs`, `Tag`, `Notice`, `EmptyState`, `ProgressBar`, `RadioCards`, `Popover` /
  `usePopover`, `MenuButton` …); `ws-dialog.jsx`; `ws-notify.jsx` (`wsToast`, `wsNotify`, `wsConfirm`); `ws-prefs.js`;
  vocabulary in `labels/*.js` (`ws-labels.js` re-exports); `manuscript-html.js`; `lib/` (`client.js`, `store-kit.js`,
  `events.js`, `format.js`, `text.js` — `countChars` = backend `count_words` —, `poll.js`, `work-id.js` …).
- `lib/client.js` owns the envelope, `X-Idempotency-Key`, `X-Operator-Ref`, the access token and the API base; views
  branch on `ApiRequestError.code` / `details`, never on message text. localStorage holds only preferences and read
  caches of backend truth (`wr-doc:*` is a write-through cache of author drafts).

## Invariants and gotchas
**Live install / config**
- A saved prompts snapshot wins over `config/prompts.yaml`: after a template edit run `sync_prompt_templates`
  (keys on `version`; keeps prompts the author edited in the UI unless `--force-text`). Never re-import a whole file.
- Model routing: the node spec in `llm_node_registry.py` is the only default. A saved models snapshot stores each
  node's provider / model choice; missing parameters resolve from the spec, so spec fixes reach configured installs. On
  a configured install a node without a saved route fails closed (`LLM_ROUTE_NOT_CONFIGURED`) until 系统配置「一键补齐」;
  routes of removed nodes show as stale until 一键补齐 / 分工 prunes them. `raise_llm_output_budget --node N --floor F
  --execute` writes an explicit override (rarely needed).
- The author's `--reload` backend hot-loads new models before the DB is migrated → `/api/*` answers 503 until
  `alembic upgrade head` (launcher restart, or in place — no restart needed afterwards).
- In a git worktree sharing a venv, CLIs and Alembic need `PYTHONPATH=<worktree>/backend/src`. Ad-hoc browser probes
  never use :5174 / :8000 (the author's live stack); `lib/client.js` honours a loopback `novel-system-api-base` override
  only when `novel-system-api-base-default` is also set (then reload).

**LLM layer and prompts**
- Every provider POST goes through `llm_accounting.execute_accounted_call` (reservation → dispatch → settlement, no
  write transaction held over network I/O); accounting is unconditional. A usage overage blocks delivery
  (`LLM_USAGE_EXCEEDS_RESERVATION`) only while the scene budget is armed. A reply carrying U+FFFD the prompt did not is
  re-sent within the retry budget (`LLM_RESPONSE_CORRUPTED_TEXT`).
- Strict LLM: no heuristic / offline fallback in product paths. Prompts render only the `inline_digests` sections
  declared in `context_budget.SECTION_SPECS`; every bundle digest key needs a render slot (`test_prompt_assembly_e2e.py`).
- Structured output: `snowflake_llm_schema.enrich_structured_schema` derives array-member `properties` from the step
  editor template (schema-enforcing relays decode property-less members as `{}`); a step-less template carries them in
  `prompts.yaml`. Count contracts (5 sentences / paragraphs / expansions) raise `StructuredCountMismatch` → one
  reasoned retry → 409; never truncate. 场景规划 is batched server-side (`SCENE_DETAIL_BATCH_SIZE`); partial or shed
  generations surface as `health.generation_notice`.

**Snowflake**
- Hard gates for materialization: 01, 02, 03, 09, 10. Backend `status == stale` is the only 需复核 signal;
  `snowflake_staleness.FIELDS_CONSUMED` lists direct-parent edges only (absent = not consumed; 07 `chapters` is not a 09
  input). `semantic_payload` ignores empty top-level values, the default `rendering_mode: full` and the scene-row
  packaging keys — otherwise echoes mint `pending_review` versions and block materialization.
- `effective_rendering_mode` is the one rendering rule (`skip` is reactive-only); `cut` and `exception_reason` are
  author-only; read triage through `snowflake_triage.latest_triage_rows`. `writer_briefs.normalize_scene_writer_brief`
  keeps only its 13 keys — snowflake beats reach prompts through `scene_structure_brief.py`. Materialization writes no
  boilerplate (`story_slots`; empty = 未规划); `forbidden_text` holds literal terms read via
  `qc_constraints.forbidden_terms`. Character ids are stored as `<project>_<raw>` and stripped for the frontend
  (`snowflake_character_ids.py`).
- Coach, directions, generate and AI triage fail closed; `author_direction_brief` is a protected prompt key. JSON
  columns mutated in place need `flag_modified`.
- FE sync (`ws-snow-sync.jsx` + `ws-snow-push.js` / `ws-snow-hydrate.js`): nothing is pushed before a successful hydrate
  in this session, a pristine step without a `lastPushed` record is never pushed, and the cache merges 「本机为准」, so
  server-side structure changes are pulled in explicitly (`SnowSync.adoptServerChapters`). The backend turns a total
  wipe of a `pending_review` draft into a new version (`snowflake_step_runs.would_wipe_story`); 构思 → 历史 restores it.

**Chapters, catalog, desks**
- Story order has one source (09 row order, `snowflake_scene_order`); `scene_seq` is the position inside its chapter and
  `renumber_scene_seq` its only writer. Chapters are contiguous slices, enforced server-side (`heal_assignment`).
  `snowflake_chapter_table` is the only writer of chapter-plan rows; the 07 table is a read-only mirror.
- Catalog chapter ids are pinned serials (`SnowflakeChapterPlan.catalog_chapter_id`, minted only by
  `SnowflakeChapteringService.catalog_chapter_id`, never reused); a re-proposed chunk keeps a chapter when ≥ half the
  scenes are shared. Runtime rows follow a moved scene (`scene_rehome.rehome_scenes`).
- Catalog scene `slug` = `scene_id`. The catalog payload is the only hand-off to the desks (`design`, `work`,
  `structure`). One focus rule: `WsCatalog.focusScene()` mirrors `catalog_labels.focus_scene_payload`. Reads never
  write (display order is compacted by the writers).
- Plan-owned design is read-only at the desks: 409 `CATALOG_SCENE_DESIGN_OWNED_BY_PLAN`,
  `CATALOG_SCENE_ORDER_OWNED_BY_PLAN`, `CATALOG_CHAPTER_ORDER_OWNED_BY_PLAN`, `CATALOG_CHAPTER_STRUCTURE_OWNED_BY_PLAN`.
  One chapter name, two doors (分章面板, 章节编排; `chapter_title_sync`).
- React approves a step with `{"sync_catalog": true}` (confirm = sync). `GET …/resync-status` answers
  `supported: false` for non-snowflake works, never a 4xx (the E2E console sweep fails on any 4xx). Cards trashed with
  their chapter share its `trashed_at` and come back on the next confirm (`catalog_trash_cascade`).

**Scene run, canon, diagnosis, style**
- Run jobs are leased (`run_job_leases`), recovered at startup and every 60 s, released on shutdown; lanes run 2 scene
  pipelines and 1 chapter at a time. Checkpoints are format v2 (hashes only; v1 products are still read);
  `run_policy="auto"` is rejected (422). Best-of-N runs only under a style-first binding.
- Chapter-level readers assemble chapter text at read time from the current scene finals; the stored chapter aggregate
  is a cache. Final approval binds the read-through to the body hash the author read; reopening needs a reason and
  cascades revocation.
- One diagnosis record (`scene_diagnosis`, stable `signal_id`); ignore lists hold signal ids and are honoured by 文学质量
  and the final gate; writes that change findings return `diagnosis_rollup`.
- Style reference: jobs are owner-token fenced and a process exit requeues them; `StylePolicy` is the only binding
  decision, `binding_config.normalize_binding_config` the only config interpreter,
  `readings.record_fidelity_reading` the only readings writer, `reference_copy_gate.py` the only copy gate (≥ 12 shared
  chars → 409 `SOURCE_SAFETY_BLOCKED`; protected terms warn). A `local_only` book needs a local model on the receiving
  node's route (else 409 `STYLE_REFERENCE_CLOUD_POLICY_BLOCKED`). Details: `docs/style-reference.md`.

**Frontend**
- `main.jsx` CSS import order is cascade semantics (`styles.css`, `ws-ui.css` first; reorder only with a visual check).
- Never pass an async predicate to Playwright's `page.waitForFunction`: it evaluates once and resolves with the settled
  value; poll async conditions on the Node side with `waitUntil` from `scripts/lib/harness.mjs`.

## Where the history lives
CLAUDE.md before the slim-down, verbatim: `docs/history/claude-md-log-2026-09.md`; snowflake phases A–Z:
`docs/snowflake-method-contract.md` §5; style reference: `docs/style-reference-v3-2026-09-23.md`, `docs/history/style/`;
what the 2026-09-29 refactor changed for the author: `docs/refactor-2026-09-29.md`.
