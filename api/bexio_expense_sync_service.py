"""BexioExpenseSyncService — replicates a Moco Purchase webhook into a Bexio bill.

Mirrors the n8n "Sync expenses from Moco to Bexio" workflow:

  1. Skip if the source Purchase has no company.
  2. Find the Bexio contact by company name; if missing, fetch full company
     data from the source Moco account and create the contact.
  3. Skip if the source Purchase carries no booking account (no
     items[0].category.credit_account).
  4. Look up the Bexio booking account by account_no (falls back to defaults).
  5. Check Bexio for an existing bill (vendor + vendor_ref). If found and not
     DRAFT, skip (n8n's "bill closed" branch); otherwise update.
  6. Upload the Moco attachment to Bexio if there's a `file_url` and the
     existing bill doesn't already carry one.
  7. POST a comment back to Moco linking to the Bexio bill so users can
     follow the trail.

HTTP transport is delegated to `BexioAPI` and `SourceMocoClient` so this class
contains only business logic and is unit-tested with fakes (see
`tests/test_bexio_expense_sync_service.py`).
"""

import logging
from typing import Any

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
)
from api.source_moco_client import SourceMocoClient
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("moco_sync")


class BexioExpenseSyncService:
    BEXIO_BILL_URL_TEMPLATE = "https://office.bexio.com/index.php/kb_bill/list#/show/{id}"

    def __init__(self, *, bexio: BexioAPI, source_moco: SourceMocoClient,
                 source_account_url: str,
                 telegram: TelegramNotifier | None = None):
        self._bexio = bexio
        self._source_moco = source_moco
        self._source_account_url = source_account_url
        # Optional: when present, the skip branches below DM a Telegram chat
        # with entity context, mirroring the n8n "...Notification to Telegram"
        # nodes. Left optional so service unit tests can omit it.
        self._telegram = telegram

    def sync(self, body: dict) -> dict[str, Any]:
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

        return {"action": action, "bill_id": bill_id,
                "contact_id": contact.get("id")}

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
        moco_company = self._source_moco.get_company(company_id)
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
        content = self._source_moco.download_file(file_url)
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
        has_iban = bool(body.get("iban"))
        first_item = (body.get("items") or [{}])[0]
        bill_date = body.get("date")
        due_date = body.get("due_date") or _add_days(bill_date, 30)
        gross = body.get("gross_total")
        title = _truncate(body.get("title") or "", 80)
        line_title = _truncate(first_item.get("title") or "", 80)

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
            "payment": _build_payment(body, contact, has_iban=has_iban),
            "attachment_ids": [attachment_uuid] if attachment_uuid else [],
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

    # --- telegram skip notifications ----------------------------------------

    def _purchase_url(self, source_id: int | None) -> str:
        return (f"https://{self._source_account_url}.mocoapp.com"
                f"/purchases/{source_id}")

    def _notify_skip_with_entity(self, reason: str, body: dict) -> None:
        """Ports the n8n "No company / No Account Notification to Telegram"
        nodes: a not-synced expense with the entity context attached."""
        if not self._telegram:
            return
        company = (body.get("company") or {}).get("name") or ""
        self._telegram.notify(
            "Expense in Moco not synced to Bexio:\n"
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
            f"Expense in Moco not synced to Bexio: {self._purchase_url(body.get('id'))}\n"
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
            self._source_moco.post_comment(commentable_id=source_id,
                                           commentable_type="Purchase",
                                           text=text)
        except Exception:
            # Don't fail the whole sync if commenting back to Moco fails —
            # the bill is already created/updated in Bexio.
            logger.exception("expense sync: comment back to Moco failed source_id=%s",
                             source_id)


# --- helpers ----------------------------------------------------------------

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


def _build_payment(body: dict, contact: dict, *, has_iban: bool) -> dict:
    bill_date = body.get("date")
    due_date = body.get("due_date") or _add_days(bill_date, 20)
    gross = body.get("gross_total")
    receipt_id = body.get("receipt_identifier")
    company_name = (body.get("company") or {}).get("name") or ""

    if has_iban:
        payment = {
            "type": "QR" if body.get("reference") else "IBAN",
            "bank_account_id": BANK_ACCOUNT_ID,
            "fee": "BY_SENDER",
            "execution_date": due_date,
            "amount": gross,
            "exchange_rate": 1,
            "iban": body.get("iban"),
            "name": company_name,
            "address": contact.get("street_name") or "Strasse",
            "street": contact.get("street_name") or "Strasse",
            "house_no": contact.get("house_number") or "1",
            "postcode": contact.get("postcode") or "0000",
            "city": contact.get("city") or "Unknown",
            "country_code": "CH",
            "salary_payment": False,
            "note": body.get("info") or "-",
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
            "note": _truncate(" - ".join(p for p in (company_name,
                                                    receipt_id or "",
                                                    body.get("info") or "")
                                         if p is not None), 80),
        }

    if receipt_id:
        payment["message"] = receipt_id
        payment["booking_text"] = receipt_id
    if body.get("reference"):
        payment["reference_no"] = body["reference"]

    return payment


def _add_days(iso_date: str | None, days: int) -> str | None:
    if not iso_date:
        return None
    import datetime as dt
    parsed = dt.date.fromisoformat(iso_date)
    return (parsed + dt.timedelta(days=days)).isoformat()


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
