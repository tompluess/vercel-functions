"""
Vercel Functions entrypoint.

  GET  /                         — health check
  POST /api/moco-sync            — Moco Activity webhook receiver (Moco -> Moco)
  POST /api/bexio-expense-sync   — Moco Purchase webhook receiver (Moco -> Bexio bill)
  POST /api/bexio-invoice-sync   — Moco Invoice webhook receiver (Moco -> Bexio invoice)
  POST /api/brevo-contact-sync   — Moco Contact webhook receiver (Moco -> Brevo)

This file is intentionally thin: it parses the request, delegates auth to
`MocoWebhookValidator`, and hands the parsed body to the appropriate service.
"""

import json
import logging
import os
from typing import Any
from urllib import error as urlerror

from fastapi import FastAPI, HTTPException, Request

from api.bexio_api import BexioAPI
from api.bexio_expense_sync_service import BexioExpenseSyncService
from api.bexio_invoice_sync_service import BexioInvoiceSyncService
from api.brevo_api import BrevoAPI
from api.brevo_contact_sync_service import BrevoContactSyncService
from api.moco_api import MocoAPI
from api.moco_sync_service import MocoSyncService, TargetNotFoundError
from api.moco_webhook_validator import MocoWebhookValidator
from api.source_moco_client import SourceMocoClient
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("moco_sync")
logging.basicConfig(level=logging.INFO)

MAX_BODY_BYTES = 64 * 1024

# Telegram error notifications are wired into every endpoint, so these are
# required everywhere (see _notify_failure / the expense skip notifications).
REQUIRED_ENV_TELEGRAM = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]

REQUIRED_ENV_MOCO_SYNC = [
    "MOCO_WEBHOOK_SECRET", "MOCO_SOURCE_ACCOUNT_URL", "MOCO_USER_ID_FILTER",
    "MOCO_TARGET_SUBDOMAIN", "MOCO_TARGET_API_KEY", "MOCO_TARGET_COMPANY_ID",
    "MOCO_TARGET_DEFAULT_PROJECT_ID", "MOCO_TARGET_DEFAULT_TASK_ID",
    *REQUIRED_ENV_TELEGRAM,
]

REQUIRED_ENV_BEXIO_SYNC = [
    "MOCO_WEBHOOK_SECRET", "MOCO_SOURCE_ACCOUNT_URL",
    "MOCO_SOURCE_API_KEY", "BEXIO_API_TOKEN",
    *REQUIRED_ENV_TELEGRAM,
]

REQUIRED_ENV_BREVO_SYNC = [
    "MOCO_WEBHOOK_SECRET", "MOCO_SOURCE_ACCOUNT_URL",
    "MOCO_SOURCE_API_KEY", "BREVO_API_KEY", "BREVO_LIST_ID",
    *REQUIRED_ENV_TELEGRAM,
]

app = FastAPI()


@app.get("/")
def hello() -> dict[str, str]:
    return {"message": "Hello, World!"}


@app.post("/api/moco-sync")
async def moco_sync_webhook(request: Request) -> dict[str, Any]:
    cfg = _require_env(REQUIRED_ENV_MOCO_SYNC)
    raw = await _read_body(request)
    _verify_moco_auth(cfg, request, raw)

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

    body = _parse_json(raw)

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

    api = MocoAPI(
        subdomain=cfg["MOCO_TARGET_SUBDOMAIN"],
        api_key=cfg["MOCO_TARGET_API_KEY"],
        company_id=cfg["MOCO_TARGET_COMPANY_ID"],
    )
    service = MocoSyncService(
        api=api,
        default_project_id=int(cfg["MOCO_TARGET_DEFAULT_PROJECT_ID"]),
        default_task_id=int(cfg["MOCO_TARGET_DEFAULT_TASK_ID"]),
        source_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
    )
    notifier = _build_notifier(cfg)
    dispatch = {"create": service.sync_create,
                "update": service.sync_update,
                "delete": service.sync_delete}
    try:
        result = dispatch[event](body)
    except TargetNotFoundError as e:
        # Application-level mismatch: the source activity has no counterpart in
        # the target. A retry can't fix it, so notify and ACK with 200 to stop
        # Moco retrying (the alert now carries the visibility the 404 used to).
        logger.warning("delete: target_not_found remote_id=%s", e)
        return _app_error(notifier, request, event, body, f"target_not_found: {e}")
    except urlerror.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        logger.error("target API error: %s %s", e.code, err_body)
        detail = f"target_error: {e.code} {err_body}"
        if e.code >= 500:
            # Upstream server error — transient infrastructure; let Moco retry.
            raise HTTPException(502, detail)
        return _app_error(notifier, request, event, body, detail)
    except urlerror.URLError as e:
        # Upstream unreachable — infrastructure; let Moco retry (no Telegram
        # ping, or a flapping upstream would spam the chat on every retry).
        logger.error("target unreachable: %s", e)
        raise HTTPException(502, "target_unreachable")
    except Exception as e:
        logger.exception("Exception: %s, Error on request with payload=%s", e, body)
        return _app_error(notifier, request, event, body, f"internal_error: {e}")

    logger.info("synced source=%s event=%s result=%s",
                body.get("id"), event, result)
    return {"ok": True, "event": event, **result}


@app.post("/api/bexio-expense-sync")
async def bexio_expense_sync_webhook(request: Request) -> dict[str, Any]:
    return await _handle_moco_dispatch_webhook(
        request,
        required_env=REQUIRED_ENV_BEXIO_SYNC,
        expected_target="Purchase",
        upstream_label="bexio",
        build_service=lambda cfg, notifier: BexioExpenseSyncService(
            bexio=BexioAPI(api_token=cfg["BEXIO_API_TOKEN"]),
            source_moco=SourceMocoClient(
                subdomain=cfg["MOCO_SOURCE_ACCOUNT_URL"],
                api_key=cfg["MOCO_SOURCE_API_KEY"],
            ),
            source_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
            telegram=notifier,
        ),
    )


@app.post("/api/bexio-invoice-sync")
async def bexio_invoice_sync_webhook(request: Request) -> dict[str, Any]:
    return await _handle_moco_dispatch_webhook(
        request,
        required_env=REQUIRED_ENV_BEXIO_SYNC,
        expected_target="Invoice",
        upstream_label="bexio",
        build_service=lambda cfg, notifier: BexioInvoiceSyncService(
            bexio=BexioAPI(api_token=cfg["BEXIO_API_TOKEN"]),
            source_moco=SourceMocoClient(
                subdomain=cfg["MOCO_SOURCE_ACCOUNT_URL"],
                api_key=cfg["MOCO_SOURCE_API_KEY"],
            ),
            source_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
            telegram=notifier,
        ),
    )


@app.post("/api/brevo-contact-sync")
async def brevo_contact_sync_webhook(request: Request) -> dict[str, Any]:
    return await _handle_moco_dispatch_webhook(
        request,
        required_env=REQUIRED_ENV_BREVO_SYNC,
        expected_target="Contact",
        upstream_label="brevo",
        build_service=lambda cfg, notifier: BrevoContactSyncService(
            brevo=BrevoAPI(api_key=cfg["BREVO_API_KEY"]),
            source_moco=SourceMocoClient(
                subdomain=cfg["MOCO_SOURCE_ACCOUNT_URL"],
                api_key=cfg["MOCO_SOURCE_API_KEY"],
            ),
            source_account_url=cfg["MOCO_SOURCE_ACCOUNT_URL"],
            list_id=int(cfg["BREVO_LIST_ID"]),
        ),
    )


async def _handle_moco_dispatch_webhook(
    request: Request, *, required_env: list[str], expected_target: str,
    upstream_label: str, build_service,
) -> dict[str, Any]:
    """Shared Moco-webhook → external-system dispatch pipeline.

    The three webhooks-to-external endpoints (two Bexio, one Brevo) differ
    only in (a) the required env vars, (b) the expected `x-moco-target`
    header, (c) which service is constructed (via `build_service(cfg,
    notifier)`), and (d) the error label surfaced on upstream failure.
    Everything else — auth, parse, envelope handling, error mapping, and the
    Telegram failure notification — is shared.
    """
    cfg = _require_env(required_env)
    raw = await _read_body(request)
    _verify_moco_auth(cfg, request, raw)

    target = request.headers.get("x-moco-target")
    event = request.headers.get("x-moco-event")
    if target != expected_target:
        logger.warning("rejecting: unexpected target=%s expected=%s event=%s",
                       target, expected_target, event)
        raise HTTPException(422, f"unexpected_target: {target}")
    if event not in ("create", "update"):
        logger.warning("rejecting: event_not_handled event=%s target=%s",
                       event, target)
        raise HTTPException(422, f"event_not_handled: {event}")

    parsed = _parse_json(raw)
    # Moco wraps the actual entity in a `body` key for these workflows
    # (matches the n8n "Extract Purchase" code node), but the moco-sync
    # webhook ships the entity at the top level. Support both shapes.
    body = parsed.get("body") if isinstance(parsed.get("body"), dict) else parsed

    notifier = _build_notifier(cfg)
    service = build_service(cfg, notifier)
    try:
        result = service.sync(body)
    except urlerror.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        logger.error("%s API error: %s %s", upstream_label, e.code, err_body)
        detail = f"{upstream_label}_error: {e.code} {err_body}"
        if e.code >= 500:
            # Upstream server error — transient infrastructure; let Moco retry.
            raise HTTPException(502, detail)
        return _app_error(notifier, request, event, body, detail)
    except urlerror.URLError as e:
        # Upstream unreachable — infrastructure; let Moco retry (no Telegram
        # ping, or a flapping upstream would spam the chat on every retry).
        logger.error("upstream unreachable: %s", e)
        raise HTTPException(502, "upstream_unreachable")
    except Exception as e:
        logger.exception("Exception: %s, Error on request with payload=%s", e, body)
        return _app_error(notifier, request, event, body, f"internal_error: {e}")

    logger.info("%s sync target=%s event=%s source=%s result=%s",
                upstream_label, target, event, body.get("id"), result)
    return {"ok": True, "event": event, **result}


def _require_env(keys: list[str]) -> dict[str, str]:
    cfg = {k: os.environ.get(k, "") for k in keys}
    if not all(cfg.values()):
        missing = [k for k, v in cfg.items() if not v]
        logger.error("missing required env vars: %s", missing)
        raise HTTPException(500, "server_misconfigured")
    return cfg


def _build_notifier(cfg: dict[str, str]) -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=cfg["TELEGRAM_BOT_TOKEN"],
        chat_id=cfg["TELEGRAM_CHAT_ID"],
    )


def _app_error(notifier: TelegramNotifier, request: Request, event: str,
               body: Any, detail: str) -> dict[str, Any]:
    """Handle an application error: a failure a webhook retry cannot fix.

    Such failures (the upstream rejected our request with a 4xx, an unexpected
    internal exception, a source↔target mismatch) used to surface as a non-2xx
    so they'd show in Moco's delivery log — but that also made Moco retry the
    request forever. Now that Telegram carries the visibility, we instead fire
    a best-effort alert and ACK with **HTTP 200** (`ok=false`) so Moco stops
    retrying. Infrastructure failures (upstream unreachable / 5xx) take the
    5xx path instead, since for those a retry can actually succeed.

    Ports the n8n "Error Trigger → Send Error to Telegram" handler.
    """
    source_id = body.get("id") if isinstance(body, dict) else None
    notifier.notify(
        "❌ vercel-functions sync failed\n"
        f"- Endpoint: {request.url.path}\n"
        f"- Event: {event}\n"
        f"- Source ID: {source_id}\n"
        f"- Error: {detail}"
    )
    return {"ok": False, "event": event, "error": detail}


async def _read_body(request: Request) -> bytes:
    raw = await request.body()
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "invalid_content_length")
    return raw


def _verify_moco_auth(cfg: dict[str, str], request: Request, raw: bytes) -> None:
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


def _parse_json(raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid_json")
