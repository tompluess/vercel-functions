"""
Vercel Functions entrypoint.

  GET  /                — health check
  POST /api/moco-sync   — Moco Activity create/update/delete webhook receiver

This file is intentionally thin: it parses the request, delegates auth to
`MocoWebhookValidator`, and hands the parsed activity to `MocoSyncService`.
"""

import json
import logging
import os
from typing import Any
from urllib import error as urlerror

from fastapi import FastAPI, HTTPException, Request

from api.moco_sync_service import MocoSyncService
from api.moco_webhook_validator import MocoWebhookValidator

logger = logging.getLogger("moco_sync")
logging.basicConfig(level=logging.INFO)

MAX_BODY_BYTES = 64 * 1024

REQUIRED_ENV = [
    "MOCO_WEBHOOK_SECRET", "MOCO_SOURCE_ACCOUNT_URL", "MOCO_USER_ID_FILTER",
    "MOCO_TARGET_SUBDOMAIN", "MOCO_TARGET_API_KEY", "MOCO_TARGET_COMPANY_ID",
    "MOCO_TARGET_DEFAULT_PROJECT_ID", "MOCO_TARGET_DEFAULT_TASK_ID",
]

app = FastAPI()


@app.get("/")
def hello() -> dict[str, str]:
    return {"message": "Hello, World!"}


@app.post("/api/moco-sync")
async def moco_sync_webhook(request: Request) -> dict[str, Any]:
    cfg = {k: os.environ.get(k, "") for k in REQUIRED_ENV}
    if not all(cfg.values()):
        logger.error("missing required env vars")
        raise HTTPException(500, "server_misconfigured")

    raw = await request.body()
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "invalid_content_length")

    validator = MocoWebhookValidator(
        secret=cfg["MOCO_WEBHOOK_SECRET"],
        expected_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
    )
    if not validator.verify_signature(raw, request.headers.get("x-moco-signature", "")):
        logger.warning("signature mismatch")
        raise HTTPException(401, "invalid_signature")
    if not validator.timestamp_fresh(request.headers.get("x-moco-timestamp", "")):
        logger.warning("timestamp out of window")
        raise HTTPException(401, "timestamp_out_of_window")
    if not validator.account_matches(request.headers.get("x-moco-account-url", "")):
        logger.warning("wrong source account")
        raise HTTPException(401, "wrong_source_account")
    target = request.headers.get("x-moco-target")
    event = request.headers.get("x-moco-event")
    if target != "Activity":
        logger.warning("rejecting: not_activity_target (target=%s event=%s)",
                       target, event)
        raise HTTPException(422, f"not_activity_target: {target}")
    if event not in ("create", "update", "delete"):
        logger.warning("rejecting: event_not_handled (event=%s target=%s)",
                       event, target)
        raise HTTPException(422, f"event_not_handled: {event}")

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid_json")

    # Skip user filtering on delete — Moco's delete webhook body is just
    # {id}, so there's no user.id to read. Safety is preserved by the
    # namespaced-id lookup in sync_delete: we'll only ever delete a target
    # activity that we ourselves created, which already passed the filter on
    # create/update.
    if event != "delete":
        user_id = (body.get("user") or {}).get("id")
        if str(user_id) != cfg["MOCO_USER_ID_FILTER"]:
            logger.warning("rejecting: user_filter (user_id=%s body_keys=%s)",
                           user_id, sorted(body.keys()))
            raise HTTPException(422, f"user_filter: {user_id}")

    service = MocoSyncService(
        target_subdomain=cfg["MOCO_TARGET_SUBDOMAIN"],
        target_api_key=cfg["MOCO_TARGET_API_KEY"],
        target_company_id=cfg["MOCO_TARGET_COMPANY_ID"],
        default_project_id=int(cfg["MOCO_TARGET_DEFAULT_PROJECT_ID"]),
        default_task_id=int(cfg["MOCO_TARGET_DEFAULT_TASK_ID"]),
        source_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
    )
    dispatch = {"create": service.sync_create,
                "update": service.sync_update,
                "delete": service.sync_delete}
    try:
        result = dispatch[event](body)
    except urlerror.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        logger.error("target API error: %s %s", e.code, err_body)
        raise HTTPException(502, f"target_error: {e.code} {err_body}")
    except urlerror.URLError as e:
        logger.error("target unreachable: %s", e)
        raise HTTPException(502, "target_unreachable")
    except Exception as e:
        logger.exception("Exception: %s, Error on request with payload=%s", e, body)
        raise HTTPException(500, f"internal_error: {e}")

    logger.info("synced source=%s event=%s result=%s",
                body.get("id"), event, result)
    return {"ok": True, "event": event, **result}
