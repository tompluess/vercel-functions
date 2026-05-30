"""Unit tests for MocoWebhookValidator."""

import time

from api.moco_webhook_validator import MocoWebhookValidator


def make_validator(secret: str = "shh", account: str = "solar") -> MocoWebhookValidator:
    return MocoWebhookValidator(secret=secret, expected_account_url=account)


def test_verify_signature_accepts_matching_hmac():
    v = make_validator()
    body = b'{"hello":"world"}'
    import hashlib, hmac
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert v.verify_signature(body, sig) is True


def test_verify_signature_rejects_mismatched_hmac():
    v = make_validator()
    assert v.verify_signature(b'{"hello":"world"}', "deadbeef") is False


def test_verify_signature_rejects_empty_header():
    v = make_validator()
    assert v.verify_signature(b'{"hello":"world"}', "") is False


def test_timestamp_fresh_accepts_now():
    v = make_validator()
    assert v.timestamp_fresh(str(int(time.time() * 1000))) is True


def test_timestamp_fresh_rejects_old_timestamp():
    v = make_validator()
    too_old = int(time.time() * 1000) - 10 * 60 * 1000
    assert v.timestamp_fresh(str(too_old)) is False


def test_timestamp_fresh_rejects_non_numeric():
    v = make_validator()
    assert v.timestamp_fresh("not-a-number") is False


def test_account_matches():
    v = make_validator(account="solar")
    assert v.account_matches("solar") is True
    assert v.account_matches("other") is False
    assert v.account_matches("") is False
