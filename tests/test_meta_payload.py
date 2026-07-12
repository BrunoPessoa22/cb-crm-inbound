"""meta_payload must always return valid JSON, never a sliced string."""
from __future__ import annotations

import json

from app.actions import _MAX_META_CHARS, MAX_FULL_TEXT_CHARS, meta_payload


def test_small_meta_passthrough():
    meta = {"subject": "Re: oi", "preview": "curto", "full_text": "curto"}
    payload = meta_payload(meta)
    assert json.loads(payload) == meta


def test_full_text_capped_at_limit_survives():
    meta = {
        "subject": "Re: proposta",
        "preview": "p" * 300,
        "full_text": "x" * MAX_FULL_TEXT_CHARS,
        "message_id": "<abc@example.com>",
        "source": "cb-crm-inbound",
    }
    payload = meta_payload(meta)
    parsed = json.loads(payload)  # must not raise
    assert parsed["full_text"].startswith("x")
    assert len(payload) <= _MAX_META_CHARS


def test_oversized_full_text_trimmed_not_sliced_json():
    # Escaping inflation: lots of quotes double in size when dumped.
    meta = {"subject": "s", "full_text": '"' * 20000}
    payload = meta_payload(meta)
    parsed = json.loads(payload)  # valid JSON is the whole point
    assert parsed.get("full_text_truncated") is True
    assert len(payload) <= _MAX_META_CHARS


def test_last_resort_drops_full_text_entirely():
    # Even when trimming cannot fit (huge unrelated field), the payload
    # stays valid JSON and full_text is dropped, never a broken slice.
    meta = {"subject": "s" * 20000, "full_text": "y" * 5000}
    payload = meta_payload(meta)
    parsed = json.loads(payload)
    assert "full_text" not in parsed or parsed["full_text"] == ""
    assert parsed["full_text_truncated"] is True
