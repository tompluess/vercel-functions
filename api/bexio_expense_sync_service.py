"""BexioExpenseSyncService — replicates a Moco Purchase webhook into a Bexio bill.

Mirrors the n8n "Sync expenses from Moco to Bexio" workflow:

  1. Skip if the Moco Purchase has no company.
  2. Find the Bexio contact by company name; if missing, fetch full company
     data from the Moco account and create the contact.
  3. Skip if the Moco Purchase carries no booking account (no
     items[0].category.credit_account).
  4. Look up the Bexio booking account by account_no (falls back to defaults).
  5. Check Bexio for an existing bill (vendor + vendor_ref). If found and not
     DRAFT, skip (n8n's "bill closed" branch); otherwise update.
  6. Upload the Moco attachment to Bexio if there's a `file_url` and the
     existing bill doesn't already carry one.
  7. POST a comment back to Moco linking to the Bexio bill so users can
     follow the trail.

HTTP transport is delegated to `BexioAPI` and `MocoClient` so this class
contains only business logic and is unit-tested with fakes (see
`tests/test_bexio_expense_sync_service.py`).
"""

import logging
from typing import Any
from urllib import error as urlerror

from api.bexio_api import BexioAPI
from api.bexio_config import (
    BANK_ACCOUNT_ID,
    CONTACT_PARTNER_ID,
    CONTACT_TYPE_ID_COMPANY,
    COUNTRY_ID_CH,
    DEFAULT_BOOKING_ACCOUNT_NO,
    DEFAULT_TAX_ID,
    OWNER_ID,
    USER_ID,
    manual_bank_account_id,
    outgoing_payment_sender,
)
from api.moco_client import MocoClient
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("bexio_expense_sync_service")

# Cap for the payment remark (`payment.note`). Inherited from the n8n
# workflow, which truncated the MANUAL note here and left the IBAN one
# uncapped; `_payment_note` now applies it to both.
PAYMENT_NOTE_MAX_CHARS = 80

# Bexio's own limit, learned from a 400 on `POST /4.0/purchase/bills`:
#   payment.booking_text size must be between 1 and 35
# Moco purchase 4642736 (solar) carried a till-slip transaction line as
# its `receipt_identifier` — "0750 19.08.2026 16:07 0011 000171 004642",
# 40 chars — which the OCR flow reads off a Hornbach Kassenbon as the
# invoice number.
#
# `booking_text` and `message` both carry that same receipt reference, so
# the cap is applied once at the source. `message` may well tolerate more
# (the Swiss unstructured-message limit is 140), but nothing we send it
# is longer than a receipt id, so there is no value in guessing at a
# second limit we have not been told.
#
# Deliberately NOT applied to `vendor_ref`: that is the idempotency key
# `_find_existing_bill` searches on, Bexio accepted the full 40 chars in
# the same request, and truncating it would make replays miss the bill
# they should update. Nor to `reference_no` — a QR reference is exactly
# 27 digits and a truncated payment reference is a wrong payment.
PAYMENT_RECEIPT_REF_MAX_CHARS = 35


class BexioExpenseSyncService:
    BEXIO_BILL_URL_TEMPLATE = "https://office.bexio.com/index.php/kb_bill/list#/show/{id}"

    def __init__(self, *, bexio: BexioAPI, moco: MocoClient,
                 subdomain: str,
                 telegram: TelegramNotifier | None = None):
        self._bexio = bexio
        self._moco = moco
        self._subdomain = subdomain
        # Optional: when present, the skip branches below DM a Telegram chat
        # with entity context, mirroring the n8n "...Notification to Telegram"
        # nodes. Left optional so service unit tests can omit it.
        self._telegram = telegram

    def sync(self, body: dict) -> dict[str, Any]:
        # "Review pending" is the marker the /api/supplier-invoice-ocr flow
        # sets on auto-created purchases. Until a human strips the tag in
        # Moco's UI, the data is unreviewed — sync to Bexio would propagate
        # OCR mistakes (wrong supplier, wrong amount, wrong IBAN). Skip
        # silently: no Telegram alert, just an INFO log. Moco gets ok=true
        # so it doesn't retry the webhook either.
        if _has_review_pending_tag(body):
            logger.info("expense sync: skipped (Review pending tag) "
                        "source_id=%s", body.get("id"))
            return {"skipped": "review_pending"}

        company = body.get("company") or {}
        company_name = company.get("name")
        if not company_name:
            logger.warning("expense sync: skipped (no company) source_id=%s",
                           body.get("id"))
            self._notify_skip_with_entity("No company given", body)
            return {"skipped": "no_company"}

        contact = self._find_or_create_contact(company_name, company.get("id"))

        account_no = _account_no_from(body)
        if not account_no:
            logger.warning("expense sync: skipped (no account) source_id=%s",
                           body.get("id"))
            self._notify_skip_with_entity("No account given", body)
            return {"skipped": "no_account", "contact_id": contact.get("id")}

        account = self._lookup_account(account_no)

        existing_bill = self._find_existing_bill(company_name,
                                                 body.get("receipt_identifier"))
        if existing_bill and (existing_bill.get("status") or "").upper() != "DRAFT":
            logger.warning("expense sync: skipped (bill not DRAFT) bill_id=%s status=%s",
                           existing_bill.get("id"), existing_bill.get("status"))
            self._notify_bill_closed(body)
            return {"skipped": "bill_not_draft",
                    "bill_id": existing_bill.get("id"),
                    "status": existing_bill.get("status")}

        attachment_uuid = self._maybe_upload_attachment(body, existing_bill)

        payload = self._build_bill_payload(body, contact, account,
                                           attachment_uuid=attachment_uuid,
                                           existing_bill=existing_bill)

        if existing_bill:
            bill = self._bexio.update_bill(existing_bill["id"], payload)
            action = "updated"
            bill_id = existing_bill["id"]
        else:
            bill = self._bexio.create_bill(payload)
            action = "created"
            bill_id = bill.get("id")

        self._post_moco_comment(body.get("id"), bill_id, action)

        payment_id = self._try_book_and_pay(bill_id, body, contact)

        result = {"action": action, "bill_id": bill_id,
                  "contact_id": contact.get("id")}
        if payment_id is not None:
            result["payment_id"] = payment_id
        return result

    # --- contact handling ---------------------------------------------------

    def _find_or_create_contact(self, name: str, company_id: int | None) -> dict:
        results = self._bexio.search_contact_by_name(name)
        if results:
            return results[0]
        if not company_id:
            # No company id to fetch full details — fall back to a minimal
            # contact built from just the name.
            return self._bexio.create_contact({
                "contact_type_id": CONTACT_TYPE_ID_COMPANY,
                "name_1": name, "country_id": COUNTRY_ID_CH,
                "user_id": USER_ID, "owner_id": OWNER_ID,
            })
        moco_company = self._moco.get_company(company_id)
        return self._bexio.create_contact(_contact_payload_from_moco(moco_company))

    # --- account lookup -----------------------------------------------------

    def _lookup_account(self, account_no: str) -> dict:
        results = self._bexio.search_account_by_no(account_no)
        if results:
            return results[0]
        # Fall back to the configured default account so bill creation still
        # works. Logged so it's visible in Vercel logs and can be reviewed.
        logger.warning("expense sync: booking account %s not found, falling back to %s",
                       account_no, DEFAULT_BOOKING_ACCOUNT_NO)
        fallback = self._bexio.search_account_by_no(DEFAULT_BOOKING_ACCOUNT_NO)
        return fallback[0] if fallback else {"id": None, "tax_id": DEFAULT_TAX_ID}

    # --- bill lookup --------------------------------------------------------

    def _find_existing_bill(self, vendor: str, vendor_ref: str | None) -> dict | None:
        envelope = self._bexio.search_bills(vendor=vendor, vendor_ref=vendor_ref)
        bills = envelope.get("data") or []
        if not bills:
            return None
        # n8n fetches the full bill via GET /4.0/purchase/bills/{id} so the
        # update payload can preserve fields like `supplier_id`, `document_no`,
        # `split_into_line_items`, and existing `attachment_ids`.
        return self._bexio.get_bill(bills[0]["id"])

    # --- attachment ---------------------------------------------------------

    def _maybe_upload_attachment(self, body: dict,
                                 existing_bill: dict | None) -> str | None:
        file_url = body.get("file_url")
        if not file_url:
            return None
        if existing_bill and existing_bill.get("attachment_ids"):
            return None
        content = self._moco.download_file(file_url)
        filename = _attachment_filename(body)
        uploaded = self._bexio.upload_file(filename=filename, content=content)
        # Bexio's POST /3.0/files returns a list of file records (one per
        # uploaded file). Older docs / n8n pinned data show a single object,
        # so accept both shapes defensively.
        if isinstance(uploaded, list):
            return uploaded[0].get("uuid") if uploaded else None
        return uploaded.get("uuid") if isinstance(uploaded, dict) else None

    # --- bill payload -------------------------------------------------------

    def _build_bill_payload(self, body: dict, contact: dict, account: dict, *,
                            attachment_uuid: str | None,
                            existing_bill: dict | None) -> dict:
        iban = _moco_iban(body)
        has_iban = bool(iban)
        first_item = (body.get("items") or [{}])[0]
        bill_date = body.get("date")
        due_date = body.get("due_date") or _add_days(bill_date, 30)
        gross = body.get("gross_total")
        title = _bexio_text(body.get("title") or "", 80)
        line_title = _bexio_text(first_item.get("title") or "", 80)

        line_item = {
            "position": 0,
            "title": line_title,
            "amount": first_item.get("gross_total"),
            "booking_account_id": account.get("id"),
            "tax_id": account.get("tax_id", DEFAULT_TAX_ID),
        }

        payload: dict[str, Any] = {
            "supplier_id": contact.get("id"),
            "title": title,
            "contact_partner_id": CONTACT_PARTNER_ID,
            "bill_date": bill_date,
            "due_date": due_date,
            "amount_man": gross,
            "currency_code": "CHF",
            "item_net": False,
            "address": {
                "lastname_company": (body.get("company") or {}).get("name"),
                "country_code": "CH",
                "type": "COMPANY",
            },
            "line_items": [line_item],
            "discounts": [],
            "payment": _build_payment(body, contact, iban=iban),
            "attachment_ids": _resolve_attachment_ids(attachment_uuid, existing_bill),
        }

        if body.get("receipt_identifier"):
            payload["vendor_ref"] = body["receipt_identifier"]

        if existing_bill is None:
            # Creating: also set amount_calc so Bexio computes consistently.
            payload["amount_calc"] = gross
            payload["manual_amount"] = False
            if has_iban:
                payload["qr_bill_information"] = body.get("receipt_identifier") or "-"
        else:
            # Updating: preserve document_no and split flag from the existing
            # bill, and switch to manual_amount=True (matches n8n's update node).
            payload["document_no"] = existing_bill.get("document_no")
            payload["manual_amount"] = True
            payload["split_into_line_items"] = existing_bill.get("split_into_line_items")

        return payload

    # --- book + outgoing payment --------------------------------------------

    def _try_book_and_pay(self, bill_id: int | None, body: dict,
                          contact: dict) -> int | None:
        """Book the bill and create an outgoing payment in Bexio.

        Runs after both create and update so the bill ends up in BOOKED state
        with a Zahlungsausgang attached — mirrors the n8n
        "Set Bill to Booked -> Create Outgoing Payment -> Comment Booked"
        chain (which only ran on create in n8n; we extend it to update too
        because the bill_not_draft skip above already guarantees we're
        operating on a DRAFT bill).

        The bill itself is the authoritative side effect, so failure here
        does NOT fail the sync: it logs + fires a Telegram alert and
        returns None. Bexio rejects the call when a payment already exists
        for the bill — that's the expected failure shape on a replay and
        the alert surfaces it for human review.
        """
        if not bill_id:
            return None
        # MANUAL payments (no IBAN on the Moco purchase) are skipped: Bexio's
        # /4.0/payment/outgoing-payments rejects MANUAL payloads that carry
        # `message` / `booking_text` / `reference_no`, and routine cash/manual
        # bills don't benefit from the booking step anyway. Stay silent —
        # alerting on every MANUAL bill would spam the chat.
        if not _moco_iban(body):
            logger.info("expense sync: skipping book+pay for MANUAL bill "
                        "(no IBAN) bill_id=%s", bill_id)
            return None
        sender = outgoing_payment_sender()
        if not sender:
            logger.warning("expense sync: BEXIO_OUTGOING_PAYMENT_SENDER "
                           "missing/malformed, skipping book+pay bill_id=%s",
                           bill_id)
            self._notify_book_pay_failure(
                body, bill_id,
                "BEXIO_OUTGOING_PAYMENT_SENDER missing or malformed",
            )
            return None
        try:
            self._bexio.book_bill(bill_id)
            payment_payload = _build_outgoing_payment_payload(
                body, contact, sender, bill_id,
            )
            payment = self._bexio.create_outgoing_payment(payment_payload)
        except urlerror.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                err_body = "<unreadable>"
            logger.warning("expense sync: book/pay failed bill_id=%s "
                           "status=%s body=%s", bill_id, e.code, err_body)
            self._notify_book_pay_failure(body, bill_id,
                                          f"{e.code} {err_body}")
            return None
        except Exception as e:
            logger.exception("expense sync: book/pay failed bill_id=%s",
                             bill_id)
            self._notify_book_pay_failure(body, bill_id, str(e))
            return None

        execution_date = payment.get("execution_date")
        self._post_moco_booking_comment(body.get("id"), bill_id, execution_date)
        return payment.get("id")

    def _notify_book_pay_failure(self, body: dict, bill_id: int,
                                 reason: str) -> None:
        if not self._telegram:
            return
        company = (body.get("company") or {}).get("name") or ""
        self._telegram.notify(
            "⚠️ Expense in Moco synced to Bexio but booking/payment failed:\n"
            f"- Company: {company}\n"
            f"- Bill ID: {body.get('receipt_identifier') or ''}\n"
            f"- Date: {body.get('date') or ''}\n"
            f"- Bexio Bill: {self.BEXIO_BILL_URL_TEMPLATE.format(id=bill_id)}\n"
            f"- Moco Expense: {self._purchase_url(body.get('id'))}\n"
            f"Reason: {reason}"
        )

    def _post_moco_booking_comment(self, source_id: int | None,
                                   bill_id: int,
                                   execution_date: str | None) -> None:
        if not source_id:
            return
        bill_url = self.BEXIO_BILL_URL_TEMPLATE.format(id=bill_id)
        per = f" per {execution_date}" if execution_date else ""
        text = ("Lieferantenrechnung in Bexio auf <strong>gebucht</strong> "
                f"gesetzt und Zahlungsausgang erstellt{per}: {bill_url}")
        try:
            self._moco.post_comment(commentable_id=source_id,
                                           commentable_type="Purchase",
                                           text=text)
        except Exception:
            logger.exception("expense sync: booking comment to Moco failed "
                             "source_id=%s", source_id)

    # --- telegram skip notifications ----------------------------------------

    def _purchase_url(self, source_id: int | None) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/purchases/{source_id}")

    def _notify_skip_with_entity(self, reason: str, body: dict) -> None:
        """Ports the n8n "No company / No Account Notification to Telegram"
        nodes: a not-synced expense with the entity context attached."""
        if not self._telegram:
            return
        company = (body.get("company") or {}).get("name") or ""
        self._telegram.notify(
            "⚠️ Expense in Moco not synced to Bexio:\n"
            f"Reason: {reason}\n"
            "Expense:\n"
            f"- Company: {company}\n"
            f"- Bill ID: {body.get('receipt_identifier') or ''}\n"
            f"- Date: {body.get('date') or ''}\n"
            f"- Link to Moco: {self._purchase_url(body.get('id'))}"
        )

    def _notify_bill_closed(self, body: dict) -> None:
        """Ports the n8n "Bill is closed Notification to Telegram" node."""
        if not self._telegram:
            return
        self._telegram.notify(
            f"⚠️ Expense in Moco not synced to Bexio: {self._purchase_url(body.get('id'))}\n"
            "Reason: Bill is closed.\n"
            "Hint: Bill-Id might not be unique (Rechnungsnummer)"
        )

    # --- moco comment back --------------------------------------------------

    def _post_moco_comment(self, source_id: int | None, bill_id: int | None,
                           action: str) -> None:
        if not source_id or not bill_id:
            return
        verb = "aktualisiert" if action == "updated" else "erstellt"
        text = (f"Lieferantenrechnung in Bexio {verb}: "
                f"{self.BEXIO_BILL_URL_TEMPLATE.format(id=bill_id)}")
        try:
            self._moco.post_comment(commentable_id=source_id,
                                           commentable_type="Purchase",
                                           text=text)
        except Exception:
            # Don't fail the whole sync if commenting back to Moco fails —
            # the bill is already created/updated in Bexio.
            logger.exception("expense sync: comment back to Moco failed source_id=%s",
                             source_id)


# --- helpers ----------------------------------------------------------------

def _has_review_pending_tag(body: dict) -> bool:
    """True if the Moco purchase carries the 'Review pending' tag.

    Used as the very first gate in expense sync: the supplier-invoice-OCR
    flow stamps newly auto-created purchases with `["OCR", "Review pending"]`
    so they're easy to find in Moco's UI for human review. While that tag
    is on the purchase, we don't propagate to Bexio (OCR mistakes would
    book to wrong supplier/amount/IBAN). The operator strips the tag in
    Moco once they've validated the fields — a subsequent Purchase:update
    webhook then has no "Review pending" tag and sync proceeds normally.
    Match is case-insensitive + whitespace-trimmed to absorb hand-typed
    variants the operator might create.
    """
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        return False
    return any(isinstance(t, str)
               and t.strip().casefold() == "review pending"
               for t in tags)


def _moco_iban(body: dict) -> str:
    """Moco-supplied IBAN with whitespace and other non-alphanumeric chars
    stripped, uppercased.

    Moco lets users paste IBANs with spaces (e.g. "DE89 3704 0044 ..."),
    which Bexio's /4.0/payment/outgoing-payments validates strictly and
    rejects with `400 IBAN contains illegal characters`. The bill-create
    endpoint is more lenient, so the bill itself succeeds while the payment
    blows up — normalize once at the source so both endpoints see clean
    input. Treats whitespace-only input as no IBAN, which downgrades the
    payment branch to MANUAL.
    """
    raw = body.get("iban") or ""
    return "".join(c for c in raw if c.isalnum()).upper()


def _resolve_attachment_ids(uploaded_uuid: str | None,
                            existing_bill: dict | None) -> list[str]:
    # Bexio replaces (not merges) attachment_ids on PUT — sending [] would
    # detach the existing file. Preserve what's already on the bill when we
    # didn't upload anything new. Mirrors the n8n update payload.
    if uploaded_uuid:
        return [uploaded_uuid]
    if existing_bill and existing_bill.get("attachment_ids"):
        return list(existing_bill["attachment_ids"])
    return []


def _account_no_from(body: dict) -> str | None:
    first_item = (body.get("items") or [{}])[0]
    category = first_item.get("category") or {}
    return (category.get("credit_account")
            or first_item.get("supplier_credit_number"))


def _attachment_filename(body: dict) -> str:
    company_name = (body.get("company") or {}).get("name") or "unknown"
    receipt = body.get("receipt_identifier") or ""
    date = body.get("date") or ""
    base = " ".join(p for p in (date, company_name, receipt) if p).strip()
    return f"{base}.pdf"


def _build_payment(body: dict, contact: dict, *, iban: str) -> dict:
    bill_date = body.get("date")
    due_date = body.get("due_date") or _add_days(bill_date, 20)
    gross = body.get("gross_total")
    receipt_id = body.get("receipt_identifier")
    company_name = (body.get("company") or {}).get("name") or ""

    if iban:
        payment = {
            "type": "QR" if body.get("reference") else "IBAN",
            "bank_account_id": BANK_ACCOUNT_ID,
            "fee": "BY_SENDER",
            "execution_date": due_date,
            "amount": gross,
            "exchange_rate": 1,
            "iban": iban,
            "name": company_name,
            "address": contact.get("street_name") or "Strasse",
            "street": contact.get("street_name") or "Strasse",
            "house_no": contact.get("house_number") or "1",
            "postcode": contact.get("postcode") or "0000",
            "city": contact.get("city") or "Unknown",
            "country_code": "CH",
            "salary_payment": False,
            "note": _payment_note(body),
        }
    else:
        user = body.get("user") or {}
        payment = {
            "type": "MANUAL",
            "bank_account_id": manual_bank_account_id(user.get("firstname")),
            "fee": "BY_SENDER",
            "execution_date": bill_date,
            "amount": gross,
            "name": company_name,
            "country_code": "CH",
            "salary_payment": False,
            "note": _payment_note(body),
        }

    if receipt_id:
        receipt_ref = _truncate(receipt_id, PAYMENT_RECEIPT_REF_MAX_CHARS)
        payment["message"] = receipt_ref
        payment["booking_text"] = receipt_ref
    if body.get("reference"):
        payment["reference_no"] = body["reference"]

    return payment


def _build_outgoing_payment_payload(body: dict, contact: dict,
                                    sender: dict, bill_id: int) -> dict:
    """Build the POST /4.0/payment/outgoing-payments body.

    Mirrors the n8n "Create Outgoing Payment in Bexio" jsonBody:
      - QR/IBAN/MANUAL switched by `iban` + `reference`
      - fee_type=NO_FEE only for plain IBAN (no QR reference)
      - booking_text=receipt_identifier when present
      - reference_no when a reference exists; otherwise message=receipt_identifier
    Receiver address falls back to the same Strasse/0000/1/Unknown defaults
    used in the bill's `payment` block so an incomplete Bexio contact still
    produces a valid payment payload.
    """
    iban = _moco_iban(body)
    has_iban = bool(iban)
    has_reference = bool(body.get("reference"))
    bill_date = body.get("date")
    execution_date = body.get("due_date") or _add_days(bill_date, 20)
    payment_type = ("QR" if has_iban and has_reference
                    else "IBAN" if has_iban
                    else "MANUAL")
    company_name = (body.get("company") or {}).get("name") or ""
    receipt_id = body.get("receipt_identifier")

    payload: dict[str, Any] = {
        "is_salary_payment": False,
        "amount": body.get("gross_total"),
        "bill_id": str(bill_id),
        "currency_code": "CHF",
        "exchange_rate": 1,
        "execution_date": execution_date,
        "payment_type": payment_type,
        "receiver_city": contact.get("city") or "Unknown",
        "receiver_country_code": "CH",
        "receiver_name": company_name,
        "receiver_postcode": contact.get("postcode") or "0000",
        "receiver_street": contact.get("street_name") or "Strasse",
        "receiver_house_no": contact.get("house_number") or "1",
        "sender_bank_account_id": sender.get("bank_account_id",
                                             BANK_ACCOUNT_ID),
        "sender_bank_name": sender.get("bank_name", ""),
        "sender_bc_no": sender.get("bc_no", ""),
        "sender_city": sender.get("city", ""),
        "sender_country_code": sender.get("country_code", "CH"),
        "sender_house_no": sender.get("house_no", ""),
        "sender_iban": sender.get("iban", ""),
        "sender_name": sender.get("name", ""),
        "sender_postcode": sender.get("postcode", ""),
        "sender_street": sender.get("street", ""),
    }
    if has_iban:
        payload["receiver_iban"] = iban
        if not has_reference:
            payload["fee_type"] = "NO_FEE"
    receipt_ref = (_truncate(receipt_id, PAYMENT_RECEIPT_REF_MAX_CHARS)
                   if receipt_id else None)
    if receipt_ref:
        payload["booking_text"] = receipt_ref
    if has_reference:
        payload["reference_no"] = body["reference"]
    elif receipt_ref:
        payload["message"] = receipt_ref
    return payload


def _add_days(iso_date: str | None, days: int) -> str | None:
    if not iso_date:
        return None
    import datetime as dt
    parsed = dt.date.fromisoformat(iso_date)
    return (parsed + dt.timedelta(days=days)).isoformat()


def _payment_note(body: dict) -> str:
    """The remark Bexio shows against the payment, for BOTH payment types.

    The Moco purchase `title` is the best text available: on an OCR'd
    purchase it is `InvoiceData.position_title`, which folds a manual
    upload's operator subject — the *business purpose*, e.g. "Mittagessen
    20.8." — into the document's own description. Nothing else on the
    purchase says what the expense was actually for, which is exactly
    what someone approving a payment in Bexio needs to see.

    Falls back to composing supplier / Belegnummer / Zahlungszweck for a
    purchase with no title. Empty parts are dropped: the previous version
    filtered on `is not None`, but its parts were `x or ""` and so never
    None, which left dangling separators — an OCR'd card receipt with an
    unmatched supplier and no Zahlungszweck produced `" - 000047 - "`.

    Both branches used to differ here, and the IBAN/QR one read only
    Moco's `info` (the QR-bill Zahlungszweck). That field is empty on
    most invoices, so in practice every QR payment carried the bare "-"
    placeholder. See `specs/SPEC_manual_upload_subject.md` D6.
    """
    title = (body.get("title") or "").strip()
    if title:
        return _bexio_text(title, PAYMENT_NOTE_MAX_CHARS)

    parts = ((body.get("company") or {}).get("name") or "",
             body.get("receipt_identifier") or "",
             body.get("info") or "")
    composed = " - ".join(p.strip() for p in parts if p and p.strip())
    # "-" as the last resort rather than "": the n8n workflow this ports
    # used it, so a non-empty note may well be required by Bexio, and a
    # remark nobody reads is not the place to find out.
    return _bexio_text(composed, PAYMENT_NOTE_MAX_CHARS) or "-"


# Unicode dashes Bexio rejects on its text fields, mapped to the plain
# ASCII hyphen. The OCR model writes German prose and reaches for an em
# dash freely, so normalizing at the Bexio boundary is more reliable than
# instructing the prompt alone — the separator between an operator
# subject and the document description is only the *commonest* source,
# not the only one. Moco keeps whatever the model wrote; this is a
# translation for one downstream system, not a correction.
_DASHES = str.maketrans({
    "\u2014": "-",  # — em dash
    "\u2013": "-",  # – en dash
    "\u2012": "-",  # ‒ figure dash
    "\u2015": "-",  # ― horizontal bar
    "\u2212": "-",  # − minus sign
})


def _bexio_text(value: str, max_len: int) -> str:
    """Normalize dashes, collapse the whitespace they leave, then truncate.

    Every free-text field we send Bexio goes through here. Truncation
    happens last so the cap counts the characters Bexio actually
    receives.
    """
    cleaned = " ".join(value.translate(_DASHES).split())
    return _truncate(cleaned, max_len)


def _truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len]


def _contact_payload_from_moco(moco_company: dict) -> dict:
    """Translate a Moco /companies/{id} response into a Bexio contact payload.

    Address parsing mirrors the n8n workflow's split-on-newline heuristic:
    line 1 = company name (ignored, we use `name` instead), line 2 = street +
    number, line 3 = postcode + city.
    """
    name = moco_company.get("name") or ""
    address_lines = (moco_company.get("address") or "").split("\n")
    street_line = address_lines[1] if len(address_lines) > 1 else ""
    city_line = address_lines[2] if len(address_lines) > 2 else ""
    street_parts = street_line.split(" ", 1)
    city_parts = city_line.split(" ", 1)
    return {
        "contact_type_id": CONTACT_TYPE_ID_COMPANY,
        "name_1": name,
        "street_name": street_parts[0] if street_parts else "",
        "house_number": street_parts[1] if len(street_parts) > 1 else "",
        "postcode": city_parts[0] if city_parts else "",
        "city": city_parts[1] if len(city_parts) > 1 else "",
        "country_id": COUNTRY_ID_CH,
        "mail": moco_company.get("email") or "",
        "phone_fixed": moco_company.get("phone") or "",
        "url": moco_company.get("website") or "",
        "remarks": (moco_company.get("address") or "") + "\n\nAdded from Moco via vercel-functions",
        "user_id": USER_ID,
        "owner_id": OWNER_ID,
    }
