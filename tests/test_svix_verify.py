import base64
import hashlib
import hmac
import time

import pytest

from app.svix_verify import SignatureError, verify

SECRET_RAW = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_RAW).decode()


def _sign(payload: bytes, svix_id: str, ts: int) -> str:
    signed = f"{svix_id}.{ts}.".encode() + payload
    sig = base64.b64encode(
        hmac.new(SECRET_RAW, signed, hashlib.sha256).digest()
    ).decode()
    return f"v1,{sig}"


def test_valid_signature() -> None:
    payload = b'{"type":"email.received"}'
    ts = int(time.time())
    verify(SECRET, payload, "msg_1", str(ts), _sign(payload, "msg_1", ts))


def test_valid_among_multiple_signatures() -> None:
    payload = b"{}"
    ts = int(time.time())
    good = _sign(payload, "msg_2", ts)
    verify(SECRET, payload, "msg_2", str(ts), f"v1,AAAA {good}")


def test_bad_signature_rejected() -> None:
    ts = int(time.time())
    with pytest.raises(SignatureError):
        verify(SECRET, b"{}", "msg_3", str(ts), "v1,dGFtcGVyZWQ=")


def test_tampered_payload_rejected() -> None:
    ts = int(time.time())
    sig = _sign(b'{"a":1}', "msg_4", ts)
    with pytest.raises(SignatureError):
        verify(SECRET, b'{"a":2}', "msg_4", str(ts), sig)


def test_stale_timestamp_rejected() -> None:
    payload = b"{}"
    ts = int(time.time()) - 3600
    with pytest.raises(SignatureError):
        verify(SECRET, payload, "msg_5", str(ts), _sign(payload, "msg_5", ts))


def test_missing_secret_rejected() -> None:
    with pytest.raises(SignatureError):
        verify("", b"{}", "msg_6", str(int(time.time())), "v1,AAAA")


def test_missing_headers_rejected() -> None:
    with pytest.raises(SignatureError):
        verify(SECRET, b"{}", "", "", "")
