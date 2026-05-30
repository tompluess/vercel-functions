# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single Vercel-deployed FastAPI app exposing webhook endpoints. Currently hosts one endpoint (`POST /api/moco-sync`) that receives Moco `Activity:create` and `Activity:update` webhooks from a source Moco account and replicates them into a target Moco account.

## Architecture

`api/index.py` is the Vercel Functions entrypoint and is intentionally thin: it owns request parsing, env-var loading, and the auth/filter pipeline, then dispatches to one of two service methods. The webhook pipeline runs in a fixed order — verify HMAC signature → check timestamp freshness → check source account URL → check target is `Activity` → check event in {create, update} → parse JSON → filter by user — before any target-side work.

- `api/moco_webhook_validator.py` — `MocoWebhookValidator`: HMAC-SHA256 signature check, ±300s timestamp window, account-URL match. Pure, no I/O.
- `api/moco_sync_service.py` — `MocoSyncService`: resolves source project/task names against the target account (falls back to configured defaults when no match), builds the activity payload, and either POSTs (`sync_create`) or PUTs (`sync_update`). Uses `urllib` (no external HTTP client dependency).

The source↔target link is tracked **statelessly** via the target activity's own fields: on every sync, `remote_service` is set to the source account URL (`MOCO_SOURCE_ACCOUNT_URL`) and `remote_id` is set to `str(source.id)`. Update lookups scan `/activities?from=DATE&to=DATE` and filter by that pair. If no matching target activity is found on update, `sync_update` falls through to POST (upsert) — a missed create-webhook self-heals on the next update. The source's own `remote_id`/`remote_service` fields are overwritten, not passed through.

Both collaborators are instantiated per-request inside `index.py`; they don't share state across invocations. Required env vars are listed in `REQUIRED_ENV` in `api/index.py` — the handler fails fast with a 500 if any are missing.

Vercel routing: `vercel.json` is empty (`{}`); Vercel's Python builder auto-detects the FastAPI `app` in `api/index.py` and routes all paths through it. Do not add a `vercel.json` `routes`/`rewrites` block unless you also restructure the entrypoint.

## Commands

```bash
# Install dev deps (FastAPI + pytest + httpx) into a local venv
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Run the test suite
.venv/bin/pytest -v
.venv/bin/pytest tests/test_endpoint.py::test_happy_path_creates_target_activity  # single test

# Deploy preview
vercel deploy

# Deploy to production (do this for feature branches BEFORE merging to main; see auto-memory)
vercel --prod

# Tail production logs
vercel logs <deployment-url>

# Manage env vars (all REQUIRED_ENV keys must exist in the Vercel project)
vercel env ls
vercel env add <NAME>
vercel env pull   # writes .env.local
```

Tests live in `tests/`; JSON fixtures under `tests/fixtures/` mirror real Moco Activity webhook bodies, target `/projects` responses, and target `/activities?from=&to=` listings used by the update lookup. Outbound HTTP is stubbed via a patched `urllib.request.urlopen` in `tests/conftest.py` — no network is touched. CI runs the same `pytest` command on push and PR via `.github/workflows/test.yml`.

`requirements.txt` is empty — runtime deps are declared in `pyproject.toml`. Vercel installs from `requirements.txt` if present, otherwise from `pyproject.toml`.

## Conventions

- One class per file; the entrypoint just wires imports together (see auto-memory `feedback_one_class_per_file`).
- Stick to `urllib` for outbound HTTP unless there's a reason to add a dependency — the service currently has zero runtime deps beyond FastAPI.
- The `reference/` directory contains n8n workflow JSON exports used as behavior references for the Moco integrations being ported here. It is gitignored and should not be edited or committed.
