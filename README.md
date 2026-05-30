# vercel-functions

Serverless webhook handlers deployed to [Vercel](https://vercel.com), written in Python with [FastAPI](https://fastapi.tiangolo.com).

## What's in here

### `POST /api/moco-sync`

Receives [Activity webhooks](https://github.com/hundertzehn/mocoapp-api-docs) (`create`, `update`, `delete`) from a source [Moco](https://www.mocoapp.com) account and replicates them into a target Moco account.

The flow:

```
Source Moco                           Vercel Function                          Target Moco
─────────────                         ───────────────                          ─────────────
Activity created  ──webhook──▶  ┌─────────────────────────┐
updated, or deleted             │ 1. verify HMAC          │
                                │ 2. check timestamp      │
                                │ 3. check account        │
                                │ 4. accept event in      │
                                │    {create,update,      │
                                │     delete}             │
                                │ 5. filter by user       │
                                └────────────┬────────────┘
                                             │
                          create ────────────┤            GET /projects ──▶
                                             │            ◀── project list
                                             │            POST /activity ─▶
                                             │
                          update ────────────┤            GET /activities ▶  (for date)
                                             │            ◀── activities
                                             │            GET /projects ──▶
                                             │            ◀── project list
                                             │            PUT /activity/{id} or POST
                                             │              (upsert if not found)
                                             │
                          delete ────────────┤            GET /activities ▶  (14-day window)
                                             │            ◀── activities
                                             ▼            DELETE /activity/{id}
                                                            (404 if not found)
```

Source project and task are mapped onto the target account **by name**. If no match is found, configured defaults are used. The link between source and target activity is tracked **statelessly** by writing a namespaced `remote_id` (`{source-account}:{source-id}`) on the target activity — so updates and deletes can be located without any external database.

Useful when two related companies share an employee and time entries logged on one side need to mirror to the other.

## Architecture

- [`api/index.py`](api/index.py) — FastAPI entrypoint. Parses the request, runs the auth pipeline, delegates to the two collaborators below.
- [`api/moco_webhook_validator.py`](api/moco_webhook_validator.py) — `MocoWebhookValidator`: HMAC-SHA256 signature check, ±300s timestamp window, source-account allowlist. Pure, no I/O.
- [`api/moco_sync_service.py`](api/moco_sync_service.py) — `MocoSyncService`: resolves project/task names against the target account, builds the activity payload, calls the target Moco API. Uses `urllib` — no external HTTP client.

## Tech

- Python 3.12+, FastAPI
- [Vercel Fluid Compute](https://vercel.com/docs/fluid-compute) (Python runtime)
- Pytest with patched `urlopen` — tests touch no network
- GitHub Actions CI on push to `main` and on pull requests

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
```

## Deploy

Deployments are managed with the [Vercel CLI](https://vercel.com/docs/cli). The `main` branch auto-deploys to production.

```bash
vercel deploy        # preview deployment
vercel --prod        # production deployment from the current branch
vercel logs <url>    # tail logs for a given deployment
```

Required environment variables (configure in the Vercel project, then `vercel env pull` for local use):

| Variable | Purpose |
| --- | --- |
| `MOCO_WEBHOOK_SECRET` | Shared secret used by the source Moco account to sign webhook bodies |
| `MOCO_SOURCE_ACCOUNT_URL` | Expected `x-moco-account-url` header value |
| `MOCO_USER_ID_FILTER` | Only sync activities for this Moco user ID |
| `MOCO_TARGET_SUBDOMAIN` | `{subdomain}.mocoapp.com` of the target account |
| `MOCO_TARGET_API_KEY` | API token for the target Moco account |
| `MOCO_TARGET_COMPANY_ID` | Target company ID used to scope the `/projects` lookup |
| `MOCO_TARGET_DEFAULT_PROJECT_ID` | Fallback project ID when no project name matches |
| `MOCO_TARGET_DEFAULT_TASK_ID` | Fallback task ID when no task name matches |

## Author

[@tompluess](https://github.com/tompluess)
