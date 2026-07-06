"""MocoWebhookValidator — verifies authenticity of inbound Moco webhooks."""

import hashlib
import hmac
import time


class MocoWebhookValidator:
    """Validates Moco webhook authenticity: HMAC signature, timestamp freshness,
    and that the event originated from the expected account."""

    TIMESTAMP_WINDOW_SECONDS = 300

    def __init__(self, *, secret: str, expected_account_url: str):
        self._secret = secret.encode()
        self._expected_account_url = expected_account_url

    def verify_signature(self, raw_body: bytes, header_signature: str) -> bool:
        if not header_signature:
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header_signature)

    def timestamp_fresh(self, header_timestamp: str) -> bool:
        try:
            ts_ms = int(header_timestamp)
        except ValueError:
            return False
        window_ms = self.TIMESTAMP_WINDOW_SECONDS * 1000
        return abs(int(time.time() * 1000) - ts_ms) <= window_ms

    def account_matches(self, header_account_url: str) -> bool:
        return header_account_url == self._expected_account_url
