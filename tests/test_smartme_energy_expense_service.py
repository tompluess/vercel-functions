"""Unit tests for SmartmeEnergyExpenseService — detection rule, expense
payload shape, keep-draft paths (no attachment / unmatched / no amount),
draft deletion after create, and Telegram routing. In-memory fakes for
all collaborators."""

import base64
import io
from urllib import error as urlerror

from api.anthropic_ocr_client import EnergyBillData
from api.smartme_energy_expense_service import (
    COMMENTABLE_TYPE_DRAFT,
    TITLE_EIGENVERBRAUCH,
    TITLE_ZEV,
    SmartmeEnergyExpenseService,
    is_smartme_draft,
)
from api.smartme_project_matcher import SmartmeProjectMatcher


PDF_BYTES = b"%PDF-1.4 smartme-test"

# Mirrors the real Leimbach draft (3070959) + its billing PDF.
DRAFT_BODY = {
    "id": 3070959,
    "title": "smart-me: Ihre Energiekostenabrechnung",
    "email_from": "no-reply@smart-me.com",
    "email_body": "Objektname: Gesamtverbrauch\n"
                  "Abrechnungszeitraum: 01.01.2026 - 30.06.2026",
    "file_url": "https://data.mocoapp.com/objects/fake.pdf?sig=abc",
}

PROJECTS = [
    {"id": 947440794,
     "name": "Hauptstrasse 33, Leimbach, Solarstrom Eigenverbrauch",
     "tags": ["Contracting", "Eigenverbrauch", "Stromproduktion"]},
    {"id": 947749060, "name": "ZEV Strombezug, Blumenrain 1, Oberkirch",
     "tags": ["ZEV"]},
]


def make_bill(**overrides) -> EnergyBillData:
    base = dict(
        objekt="Gesamtverbrauch (Hauptstrasse 33 Leimbach)",
        net_amount=558.09,
        period_from="2026-01-01",
        period_to="2026-06-30",
        invoice_date="2026-07-05",
        invoice_number="10007",
        confidence=0.95,
    )
    base.update(overrides)
    return EnergyBillData(**base)


# --- fakes ------------------------------------------------------------------

class FakeMoco:
    def __init__(self, pdf_bytes: bytes = PDF_BYTES):
        self.pdf_bytes = pdf_bytes
        self.downloads: list[str] = []
        self.expenses: list[tuple[int, dict]] = []
        self.expense_error: Exception | None = None
        self.next_expense_id = 5555001
        self.comments: list[dict] = []
        self.comment_error: Exception | None = None

    def download_file(self, signed_url: str) -> bytes:
        self.downloads.append(signed_url)
        return self.pdf_bytes

    def create_project_expense(self, project_id: int, payload: dict) -> dict:
        if self.expense_error:
            raise self.expense_error
        self.expenses.append((project_id, payload))
        return {"id": self.next_expense_id, **payload}

    def post_comment(self, *, commentable_id: int, commentable_type: str,
                     text: str) -> dict:
        if self.comment_error:
            raise self.comment_error
        self.comments.append({"commentable_id": commentable_id,
                              "commentable_type": commentable_type,
                              "text": text})
        return {"id": 1}


class FakePurchaseClient:
    def __init__(self):
        self.deleted_drafts: list[int] = []
        self.delete_draft_error: Exception | None = None

    def delete_purchase_draft(self, draft_id: int) -> None:
        if self.delete_draft_error:
            raise self.delete_draft_error
        self.deleted_drafts.append(draft_id)


class FakeOcr:
    def __init__(self, result: EnergyBillData | None = None,
                 error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[bytes] = []

    def extract_energy_bill(self, pdf_bytes: bytes) -> EnergyBillData:
        self.calls.append(pdf_bytes)
        if self.error:
            raise self.error
        return self.result


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True


def build_service(*, moco=None, purchases=None, ocr=None, telegram=None,
                  projects=None, subdomain="solar"):
    return SmartmeEnergyExpenseService(
        moco=moco or FakeMoco(),
        purchase_client=purchases or FakePurchaseClient(),
        ocr=ocr or FakeOcr(result=make_bill()),
        matcher=SmartmeProjectMatcher(
            PROJECTS if projects is None else projects),
        subdomain=subdomain,
        telegram=telegram,
    )


def _http_error(code: int, body: bytes = b"boom") -> urlerror.HTTPError:
    return urlerror.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


# --- detection ---------------------------------------------------------------

def test_detects_with_title_and_body_markers():
    """Forwarded mail: email_from is the forwarder, but title + body
    markers make two signals."""
    body = dict(DRAFT_BODY, email_from="thomas@example.com")
    assert is_smartme_draft(body) is True


def test_detects_with_sender_and_body_markers():
    body = dict(DRAFT_BODY, title="FW: Abrechnung")
    assert is_smartme_draft(body) is True


def test_detects_with_title_and_sender_in_body():
    body = {"id": 1, "title": "Test: smart-me: Ihre Energiekostenabrechnung",
            "email_body": "Von: no-reply@smart-me.com"}
    assert is_smartme_draft(body) is True


def test_single_signal_is_not_enough():
    assert is_smartme_draft(
        {"id": 1, "title": "Ihre Energiekostenabrechnung"}) is False
    assert is_smartme_draft(
        {"id": 1, "email_from": "no-reply@smart-me.com"}) is False
    assert is_smartme_draft(
        {"id": 1, "email_body": "Objektname: X"}) is False


def test_detection_is_case_insensitive():
    body = {"id": 1, "title": "SMART-ME: IHRE ENERGIEKOSTENABRECHNUNG",
            "email_body": "OBJEKTNAME: GESAMTVERBRAUCH"}
    assert is_smartme_draft(body) is True


def test_detection_survives_non_string_fields():
    assert is_smartme_draft(
        {"id": 1, "title": None, "email_from": 42, "email_body": []}) is False


def test_regular_invoice_draft_is_not_detected():
    assert is_smartme_draft({
        "id": 1, "title": "Rechnung R-2026-042",
        "email_from": "billing@flyeralarm.de",
        "email_body": "Anbei Ihre Rechnung.",
    }) is False


# --- happy path ---------------------------------------------------------------

def test_happy_path_creates_expense_on_eigenverbrauch_project():
    moco = FakeMoco()
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    s = build_service(moco=moco, purchases=purchases, telegram=tg)

    result = s.process_draft(DRAFT_BODY)

    assert result["smartme"] is True
    assert result["expense_id"] == 5555001
    assert result["project_id"] == 947440794
    assert result["expense_title"] == TITLE_EIGENVERBRAUCH

    project_id, payload = moco.expenses[0]
    assert project_id == 947440794
    assert payload["date"] == "2026-07-05"
    assert payload["title"] == "Solarstrom Eigenverbrauch gemäss Beilage"
    assert payload["quantity"] == 1
    assert payload["unit"] == "Netto"
    assert payload["unit_price"] == 558.09
    assert payload["unit_cost"] == 0
    assert payload["billable"] is True
    assert payload["budget_relevant"] is True
    assert payload["service_period_from"] == "2026-01-01"
    assert payload["service_period_to"] == "2026-06-30"
    assert base64.b64decode(payload["file"]["base64"]) == PDF_BYTES
    assert payload["file"]["filename"].endswith(".pdf")

    # Draft deleted, success Telegram sent, no draft comment needed.
    assert purchases.deleted_drafts == [3070959]
    assert len(tg.messages) == 1
    assert "verbucht" in tg.messages[0]
    assert "Hauptstrasse 33" in tg.messages[0]
    assert "558.09" in tg.messages[0]
    assert "2026-01-01 – 2026-06-30" in tg.messages[0]
    assert moco.comments == []


def test_zev_project_gets_netzstrom_title():
    moco = FakeMoco()
    ocr = FakeOcr(result=make_bill(objekt="Blumenrain 1 (Oberkirch)"))
    s = build_service(moco=moco, ocr=ocr)
    result = s.process_draft(DRAFT_BODY)
    assert result["project_id"] == 947749060
    assert result["expense_title"] == TITLE_ZEV
    _, payload = moco.expenses[0]
    assert payload["title"] == "Solar- und Netzstrom gemäss Beilage"


def test_both_labels_prefer_zev_title():
    moco = FakeMoco()
    projects = [{"id": 7, "name": "Hofmatt 5, Beispielwil",
                 "tags": ["Eigenverbrauch", "ZEV"]}]
    ocr = FakeOcr(result=make_bill(objekt="Hofmatt 5 (Beispielwil)"))
    s = build_service(moco=moco, ocr=ocr, projects=projects)
    result = s.process_draft(DRAFT_BODY)
    assert result["expense_title"] == TITLE_ZEV


def test_missing_period_omits_service_period_fields():
    moco = FakeMoco()
    ocr = FakeOcr(result=make_bill(period_to=None))
    s = build_service(moco=moco, ocr=ocr)
    s.process_draft(DRAFT_BODY)
    _, payload = moco.expenses[0]
    assert "service_period_from" not in payload
    assert "service_period_to" not in payload


def test_missing_invoice_date_falls_back_to_today():
    moco = FakeMoco()
    ocr = FakeOcr(result=make_bill(invoice_date=None))
    s = build_service(moco=moco, ocr=ocr)
    s.process_draft(DRAFT_BODY)
    _, payload = moco.expenses[0]
    # Exact value is "today" — just assert it's a plausible ISO date.
    assert len(payload["date"]) == 10 and payload["date"][4] == "-"


def test_low_confidence_success_flags_review_in_telegram():
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_bill(confidence=0.4))
    s = build_service(ocr=ocr, telegram=tg)
    s.process_draft(DRAFT_BODY)
    assert "⚠️" in tg.messages[0]
    assert "bitte prüfen" in tg.messages[0]


# --- keep-draft paths ---------------------------------------------------------

def test_no_attachment_keeps_draft_and_alerts():
    moco = FakeMoco()
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_bill())
    s = build_service(moco=moco, purchases=purchases, ocr=ocr, telegram=tg)

    body = {k: v for k, v in DRAFT_BODY.items() if k != "file_url"}
    result = s.process_draft(body)

    assert result["skipped"] == "smartme_no_attachment"
    assert ocr.calls == []
    assert moco.expenses == []
    assert purchases.deleted_drafts == []
    assert len(tg.messages) == 1
    assert "nicht verbucht" in tg.messages[0]
    assert "purchases/drafts/3070959" in tg.messages[0]
    # Comment on the kept draft, typed as PurchaseDraft.
    assert moco.comments[0]["commentable_id"] == 3070959
    assert moco.comments[0]["commentable_type"] == COMMENTABLE_TYPE_DRAFT


def test_unmatched_objekt_keeps_draft_with_comment_and_alert():
    moco = FakeMoco()
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_bill(objekt="Solarpark Zermatt"))
    s = build_service(moco=moco, purchases=purchases, ocr=ocr, telegram=tg)

    result = s.process_draft(DRAFT_BODY)

    assert result["skipped"] == "smartme_project_unmatched"
    assert result["match_status"] == "no_match"
    assert result["objekt"] == "Solarpark Zermatt"
    assert moco.expenses == []
    assert purchases.deleted_drafts == []
    assert "Solarpark Zermatt" in tg.messages[0]
    comment = moco.comments[0]
    assert comment["commentable_type"] == COMMENTABLE_TYPE_DRAFT
    assert "nicht automatisch verbucht" in comment["text"]
    assert "Solarpark Zermatt" in comment["text"]


def test_ambiguous_objekt_reports_candidate_count():
    tg = FakeTelegram()
    projects = [
        {"id": 1, "name": "ZEV Strombezug, Blumenrain 1, Oberkirch",
         "tags": ["ZEV"]},
        {"id": 2, "name": "ZEV Strombezug, Blumenrain 3, Oberkirch",
         "tags": ["ZEV"]},
    ]
    ocr = FakeOcr(result=make_bill(objekt="Blumenrain (Oberkirch)"))
    s = build_service(ocr=ocr, telegram=tg, projects=projects)
    result = s.process_draft(DRAFT_BODY)
    assert result["skipped"] == "smartme_project_unmatched"
    assert result["match_status"] == "ambiguous"
    assert "2 Projekte" in tg.messages[0]


def test_missing_net_amount_keeps_draft():
    moco = FakeMoco()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_bill(net_amount=None))
    s = build_service(moco=moco, ocr=ocr, telegram=tg)
    result = s.process_draft(DRAFT_BODY)
    assert result["skipped"] == "smartme_no_net_amount"
    assert moco.expenses == []
    assert "Netto-Betrag" in tg.messages[0]


def test_failed_draft_comment_is_swallowed():
    moco = FakeMoco()
    moco.comment_error = _http_error(422)
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_bill(objekt="Solarpark Zermatt"))
    s = build_service(moco=moco, ocr=ocr, telegram=tg)
    result = s.process_draft(DRAFT_BODY)
    # Comment failure doesn't escalate — the Telegram alert already fired.
    assert result["skipped"] == "smartme_project_unmatched"
    assert len(tg.messages) == 1


# --- post-create edge cases ----------------------------------------------------

def test_expense_create_http_error_propagates():
    moco = FakeMoco()
    moco.expense_error = _http_error(422, b'{"base":["error"]}')
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases)
    try:
        s.process_draft(DRAFT_BODY)
        raise AssertionError("expected HTTPError")
    except urlerror.HTTPError:
        pass
    # Nothing deleted — the endpoint owns the error mapping.
    assert purchases.deleted_drafts == []


def test_draft_delete_404_is_silent():
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = _http_error(404)
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process_draft(DRAFT_BODY)
    assert result["expense_id"] == 5555001
    # Only the success message — no delete-failure alert for 404.
    assert len(tg.messages) == 1
    assert "verbucht" in tg.messages[0]


def test_draft_delete_failure_alerts_but_result_stays_ok():
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = _http_error(500, b"oops")
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process_draft(DRAFT_BODY)
    assert result["expense_id"] == 5555001
    delete_alerts = [m for m in tg.messages if "nicht gelöscht" in m]
    assert len(delete_alerts) == 1
    assert "purchases/drafts/3070959" in delete_alerts[0]
    assert "projects/947440794/expenses" in delete_alerts[0]


def test_works_without_telegram():
    """telegram=None (unit-test convenience + defensive prod default)."""
    s = build_service(telegram=None)
    result = s.process_draft(DRAFT_BODY)
    assert result["expense_id"] == 5555001
