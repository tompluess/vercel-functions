"""BexioInvoiceSyncService — replicates a Moco Invoice webhook into a Bexio invoice.

Mirrors the n8n "Sync invoices from Moco to Bexio" workflow:

  1. Skip unless `body.status == "sent"`. Moco fires Invoice:update on every
     edit (draft, sent, paid, …); we only sync once the invoice is sent.
  2. Fetch the Moco project to read its labels and customer.
  3. Find/create the Bexio contact for the customer.
  4. Resolve the revenue account from the project's labels.
  5. Pick a document template (first one returned by Bexio).
  6. Create the invoice in Bexio; comment back to both Moco and the new
     Bexio invoice with cross-links.
  7. Transition the invoice: ISSUE -> SET_PENDING (status: Open).

Idempotency: searches Bexio for an existing invoice by `api_reference` (= the
Moco identifier) before creating, so a replayed webhook does not duplicate.
"""

import logging
from typing import Any

from api.bexio_api import BexioAPI
from api.bexio_config import (
    BANK_ACCOUNT_ID,
    CONTACT_TYPE_ID_COMPANY,
    COUNTRY_ID_CH,
    CURRENCY_ID_CHF,
    DEFAULT_TAX_ID,
    INVOICE_UNIT_ID,
    LANGUAGE_ID_DE,
    MWST_IS_NET,
    MWST_TYPE,
    OWNER_ID,
    PAYMENT_TYPE_ID,
    USER_ID,
    resolve_revenue_account_no,
)
from api.moco_client import MocoClient
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("bexio_invoice_sync_service")


class BexioInvoiceSyncService:
    BEXIO_INVOICE_URL_TEMPLATE = "https://office.bexio.com/index.php/kb_invoice/show/id/{id}"

    def __init__(self, *, bexio: BexioAPI, moco: MocoClient,
                 subdomain: str,
                 telegram: TelegramNotifier | None = None):
        self._bexio = bexio
        self._moco = moco
        self._subdomain = subdomain
        # Optional: the no_customer skip below DMs a Telegram chat with the
        # Moco invoice link, mirroring BexioExpenseSyncService's skip
        # notifications. status_not_sent stays silent — it fires on every draft
        # edit and would spam. Left optional so service unit tests can omit it.
        self._telegram = telegram

    def sync(self, body: dict) -> dict[str, Any]:
        if body.get("status") != "sent":
            logger.info("invoice sync: skipped (status=%s) source_id=%s",
                        body.get("status"), body.get("id"))
            return {"skipped": "status_not_sent", "status": body.get("status")}

        project = self._moco.get_project(body["project_id"])
        customer = project.get("customer") or {}
        customer_name = customer.get("name") or ""
        if not customer_name:
            logger.warning("invoice sync: skipped (no customer) source_id=%s",
                           body.get("id"))
            self._notify_no_customer(body)
            return {"skipped": "no_customer"}

        contact = self._find_or_create_contact(customer_name, body, project)

        account_no = resolve_revenue_account_no(project.get("labels") or [])
        account = self._lookup_account(account_no)
        template_slug = self._first_template_slug()

        payload = self._build_invoice_payload(body, contact, account,
                                              template_slug=template_slug)

        invoice = self._bexio.create_invoice(payload)
        invoice_id = invoice.get("id")

        self._comment_invoice_in_bexio(invoice_id, body.get("id"))
        self._comment_creation_in_moco(body.get("id"), invoice_id)

        # Move DRAFT -> Open via /issue. (/set_pending is intentionally not
        # called here: the n8n workflow had both as parallel error-tolerant
        # branches, but Bexio's /2.0/kb_invoice/{id}/set_pending 404s on
        # already-issued invoices, surfacing a noisy error for no benefit.)
        try:
            self._bexio.issue_invoice(invoice_id)
        except Exception:
            logger.exception("invoice sync: /issue failed invoice_id=%s",
                             invoice_id)

        return {"action": "created", "invoice_id": invoice_id,
                "contact_id": contact.get("id"),
                "revenue_account_no": account_no}

    # --- contact ------------------------------------------------------------

    def _find_or_create_contact(self, name: str, body: dict, project: dict) -> dict:
        results = self._bexio.search_contact_by_name(name)
        if results:
            return results[0]
        moco_company = self._moco.get_company(body["customer_id"])
        billing_address = project.get("billing_address") or ""
        return self._bexio.create_contact(_contact_payload_from_moco(
            name=name, moco_company=moco_company,
            billing_address=billing_address, project=project,
        ))

    # --- account / template -------------------------------------------------

    def _lookup_account(self, account_no: str) -> dict:
        results = self._bexio.search_account_by_no(account_no)
        if results:
            return results[0]
        logger.warning("invoice sync: revenue account %s not found",
                       account_no)
        return {"id": None, "tax_id": DEFAULT_TAX_ID}

    def _first_template_slug(self) -> str:
        templates = self._bexio.list_document_templates()
        if not templates:
            return ""
        return templates[0].get("template_slug") or ""

    # --- payload ------------------------------------------------------------

    def _build_invoice_payload(self, body: dict, contact: dict, account: dict,
                               *, template_slug: str) -> dict:
        identifier = body.get("identifier") or ""
        title = _truncate(body.get("title") or "", 80)
        position_text = _truncate(f"{identifier}: {body.get('title') or ''}", 80)
        contact_address = (body.get("recipient_address") or "").replace("\n", ", ")
        moco_invoice_url = self._invoice_url(body.get("id"))

        return {
            "document_nr": identifier,
            "title": title,
            "contact_id": contact.get("id"),
            "user_id": USER_ID,
            "language_id": LANGUAGE_ID_DE,
            "bank_account_id": BANK_ACCOUNT_ID,
            "currency_id": CURRENCY_ID_CHF,
            "payment_type_id": PAYMENT_TYPE_ID,
            "mwst_type": MWST_TYPE,
            "mwst_is_net": MWST_IS_NET,
            "show_position_taxes": False,
            "is_valid_from": body.get("date"),
            "is_valid_to": body.get("due_date"),
            "contact_address_manual": contact_address,
            "api_reference": identifier,
            "template_slug": template_slug,
            "header": f"Original-Rechnung in Moco: {moco_invoice_url}",
            "footer": "-",
            "positions": [{
                "amount": "1",
                "unit_id": INVOICE_UNIT_ID,
                "account_id": account.get("id"),
                "tax_id": account.get("tax_id") or DEFAULT_TAX_ID,
                "text": position_text,
                "unit_price": str(body.get("net_total") or 0),
                "discount_in_percent": "0.000000",
                "type": "KbPositionCustom",
            }],
        }

    # --- telegram skip notification -----------------------------------------

    def _invoice_url(self, source_id: int | None) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/invoices/{source_id}")

    def _notify_no_customer(self, body: dict) -> None:
        """A sent invoice we can't resolve a customer for — the invoice
        analogue of BexioExpenseSyncService's no_company skip notification."""
        if not self._telegram:
            return
        self._telegram.notify(
            "⚠️ Invoice in Moco not synced to Bexio.\n"
            "Reason: No customer given\n"
            f"- Invoice ID: {body.get('identifier') or ''}\n"
            f"- Date: {body.get('date') or ''}\n"
            f"- Total: CHF {body.get('net_total') or ''}\n"
            f"- Invoice in Moco: {self._invoice_url(body.get('id'))}"
        )

    # --- moco / bexio cross-comments ----------------------------------------

    def _comment_invoice_in_bexio(self, invoice_id: int | None,
                                  source_id: int | None) -> None:
        if not invoice_id or not source_id:
            return
        moco_url = self._invoice_url(source_id)
        try:
            self._bexio.comment_invoice(invoice_id, {
                "text": f"Link zur Rechnung in Moco: {moco_url}",
                "user_id": USER_ID,
                "user_name": "vercel-functions",
                "is_public": False,
            })
        except Exception:
            logger.exception("invoice sync: bexio comment failed invoice_id=%s",
                             invoice_id)

    def _comment_creation_in_moco(self, source_id: int | None,
                                  invoice_id: int | None) -> None:
        if not source_id or not invoice_id:
            return
        text = (f"Rechnung in Bexio erstellt: "
                f"{self.BEXIO_INVOICE_URL_TEMPLATE.format(id=invoice_id)}")
        try:
            self._moco.post_comment(commentable_id=source_id,
                                           commentable_type="Invoice",
                                           text=text)
        except Exception:
            logger.exception("invoice sync: moco comment failed source_id=%s",
                             source_id)


# --- helpers ----------------------------------------------------------------

def _contact_payload_from_moco(*, name: str, moco_company: dict,
                               billing_address: str, project: dict) -> dict:
    """Build a Bexio contact payload from Moco source data.

    The n8n workflow parses the project's billing_address (not the company
    address) to derive street/postcode/city, since billing usually differs
    from the head-office address.
    """
    address_lines = (billing_address or "").split("\n")
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
        "mail": project.get("billing_email_to") or moco_company.get("email") or "",
        "phone_fixed": moco_company.get("phone") or "",
        "url": moco_company.get("website") or "",
        "remarks": (billing_address or "") + "\n\nAdded from Moco via vercel-functions",
        "user_id": USER_ID,
        "owner_id": OWNER_ID,
    }


def _truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len]
