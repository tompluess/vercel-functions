"""BrevoContactSyncService — replicates a Moco Contact webhook into Brevo.

Mirrors the n8n "Add Moco contacts to Brevo" workflow:

  1. Skip when the Moco contact has no work_email — Brevo identifies contacts
     by email and there is nothing to sync without one.
  2. Lookup Brevo contact by work_email.
     - Not found: create with VORNAME, NACHNAME, ADDITIONAL_INFO (today's date
       + the source Moco URL), then post a comment back to the Moco contact
       with the Brevo URL.
     - Found: update with VORNAME, NACHNAME, RESPONSIBLE_PERSON (Moco owner's
       full name), JOB_TITLE.
  3. Update the SMS attribute with the normalized mobile_phone (E.164-ish:
     strip whitespace, drop a leading single 0, keep +/00 prefixes intact).
  4. Add the contact to the configured Brevo list (idempotent on Brevo's side).

The list-add and SMS-update steps run for both create and update, matching the
n8n flow's convergence at "Update Mobile Phone in Brevo".
"""

import datetime
import logging
from typing import Any
from urllib import error as urlerror

from api.brevo_api import BrevoAPI
from api.source_moco_client import SourceMocoClient

logger = logging.getLogger("brevo_contact_sync_service")


class BrevoContactSyncService:
    BREVO_CONTACT_URL_TEMPLATE = "https://app.brevo.com/contact/index/{id}"

    def __init__(self, *, brevo: BrevoAPI, source_moco: SourceMocoClient,
                 source_account_url: str, list_id: int):
        self._brevo = brevo
        self._source_moco = source_moco
        self._source_account_url = source_account_url
        self._list_id = list_id
        # No Telegram notifier here: this service's only skip (no_work_email)
        # is a routine gate (many Moco contacts legitimately have no work
        # email), not a sync failure, so it deliberately stays silent. The
        # 5xx-vs-200 error contract is handled upstream in index.py.

    def sync(self, body: dict) -> dict[str, Any]:
        email = (body.get("work_email") or "").strip()
        if not email:
            logger.info("brevo sync: skipped (no work_email) source_id=%s",
                        body.get("id"))
            return {"skipped": "no_work_email"}

        existing = self._brevo.get_contact(email)
        if existing is None:
            result = self._create(email, body)
        else:
            result = self._update(email, body)

        self._update_mobile_phone(email, body.get("mobile_phone") or "")
        self._add_to_list(email)
        return result

    # --- create / update branches -------------------------------------------

    def _create(self, email: str, body: dict) -> dict[str, Any]:
        payload = {
            "email": email,
            "attributes": {
                "VORNAME": body.get("firstname") or "",
                "NACHNAME": body.get("lastname") or "",
                "ADDITIONAL_INFO": self._additional_info(body),
            },
            # `updateEnabled: false` keeps create-vs-update behavior explicit:
            # a race between this create and another producer should surface as
            # a 400, not a silent overwrite.
            "updateEnabled": False,
        }
        created = self._brevo.create_contact(payload)
        brevo_id = created.get("id")
        self._comment_creation_in_moco(body.get("id"), brevo_id)
        logger.info("brevo sync: created source_id=%s brevo_id=%s",
                    body.get("id"), brevo_id)
        return {"action": "created", "brevo_id": brevo_id, "email": email}

    def _update(self, email: str, body: dict) -> dict[str, Any]:
        user = body.get("user") or {}
        responsible = " ".join(
            p for p in (user.get("firstname"), user.get("lastname")) if p
        )
        payload = {
            "attributes": {
                "VORNAME": body.get("firstname") or "",
                "NACHNAME": body.get("lastname") or "",
                "RESPONSIBLE_PERSON": responsible,
                "JOB_TITLE": body.get("job_position") or "",
            },
        }
        self._brevo.update_contact(email, payload)
        logger.info("brevo sync: updated source_id=%s email=%s",
                    body.get("id"), email)
        return {"action": "updated", "email": email}

    # --- side-effect helpers ------------------------------------------------

    def _update_mobile_phone(self, email: str, raw_phone: str) -> None:
        """Set Brevo's SMS attribute with the n8n-style normalized phone.

        Failure is swallowed so a bad phone number doesn't block the list-add
        and Moco comment — the contact has already been created/updated and we
        don't want Moco to retry the whole flow over an SMS field. HTTP errors
        are logged at warning level WITHOUT a traceback (a 4xx from Brevo is
        an upstream-shape issue, not a bug here); only truly unexpected
        exceptions get logger.exception with the stack trace.
        """
        try:
            self._brevo.update_contact(email, {
                "attributes": {"SMS": _normalize_phone(raw_phone)},
            })
        except urlerror.HTTPError as e:
            _log_soft_http_failure("SMS update", email, e)
        except Exception:
            logger.exception("brevo sync: SMS update failed email=%s", email)

    def _add_to_list(self, email: str) -> None:
        """Add the email to the configured Brevo list.

        Soft-failure step (see _update_mobile_phone for rationale). Common
        non-bug HTTP responses include:
          - 400 "Contact already in list and/or doesn't exist"
            (handled idempotently in `BrevoAPI.add_to_list` and never reaches
            here; kept in mind so tests cover it)
          - 401 when the API key lacks list-management permission for this
            list_id (observed in prod for list_id=8) — surface as a clean
            warning so the operator can fix the permissions without a
            traceback misleading them into looking for a code bug
        """
        try:
            self._brevo.add_to_list(self._list_id, [email])
        except urlerror.HTTPError as e:
            _log_soft_http_failure(
                f"list add (list_id={self._list_id})", email, e,
            )
        except Exception:
            logger.exception("brevo sync: list add failed email=%s list_id=%s",
                             email, self._list_id)

    def _additional_info(self, body: dict) -> str:
        today = datetime.date.today().strftime("%-d.%-m.%Y")
        moco_url = (f"https://{self._source_account_url}.mocoapp.com/contacts/"
                    f"{body.get('id')}")
        return f"{today}, Added from Moco via vercel-functions.\n{moco_url}"

    def _comment_creation_in_moco(self, source_id: int | None,
                                  brevo_id: int | None) -> None:
        if not source_id or not brevo_id:
            return
        text = (f"Contact added to Brevo: "
                f"{self.BREVO_CONTACT_URL_TEMPLATE.format(id=brevo_id)}")
        try:
            self._source_moco.post_comment(commentable_id=source_id,
                                           commentable_type="Contact",
                                           text=text)
        except Exception:
            logger.exception("brevo sync: moco comment failed source_id=%s",
                             source_id)


def _log_soft_http_failure(step: str, email: str,
                           err: urlerror.HTTPError) -> None:
    """Tidy warning log for a side-effect HTTP error.

    Reading e.read() consumes the response stream — fine here since we
    don't re-raise. Only call this from a soft-failure code path that
    intends to keep the sync going.
    """
    try:
        body = err.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        body = "<unreadable>"
    logger.warning("brevo sync: %s failed email=%s status=%s body=%s",
                   step, email, err.code, body)


def _normalize_phone(raw: str) -> str:
    """Match the n8n JS normalization:

        ""             -> ""
        "+41 77 ..."   -> "+4177..."           (E.164 already, just trim spaces)
        "0041 ..."     -> "0041..."            (00-prefixed international)
        "0 77 777 ..." -> "777777..."          (drop the leading national zero)
        anything else  -> leave as-is

    We do not coerce arbitrary input to E.164 — the n8n behavior is intentional
    so the user can see in Brevo what they typed in Moco.
    """
    if not raw:
        return ""
    if raw.startswith(("+", "00")):
        return _strip_whitespace(raw)
    if raw.startswith("0"):
        return _strip_whitespace(raw[1:])
    return raw


def _strip_whitespace(value: str) -> str:
    return "".join(c for c in value if not c.isspace())
