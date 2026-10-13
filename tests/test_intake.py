"""Landing intake: input validation, rate limiting and auth (no database)."""
from __future__ import annotations

import os

os.environ.setdefault("CRM_DATABASE_URL", "postgresql://test@localhost/test")
os.environ.setdefault("RESEND_API_KEY", "re_test")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import intake  # noqa: E402
from app.intake import LeadIn, RateLimiter, VisitIn  # noqa: E402

LEAD = {
    "name": "Ana Teste",
    "email": "Ana@Example.com",
    "company": "Construtora Exemplo",
    "role": "Diretora",
    "phone": "",
    "vertical": "construcao",
    "token": "",
}


def test_lead_blank_optionals_become_none() -> None:
    lead = LeadIn(**LEAD)
    assert lead.phone is None
    assert lead.token is None


def test_lead_keeps_valid_phone_and_token() -> None:
    lead = LeadIn(**{**LEAD, "phone": "+55 (11) 91234-5678", "token": "abcDEF12_-"})
    assert lead.phone == "+55 (11) 91234-5678"
    assert lead.token == "abcDEF12_-"


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "not-an-email"),
        ("name", "A"),
        ("vertical", "Construcao!"),
        ("phone", "call me"),
        ("token", "short"),
    ],
)
def test_lead_rejects_bad_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        LeadIn(**{**LEAD, field: value})


def test_lead_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        LeadIn(**{**LEAD, "website": "bot"})


def test_visit_requires_token() -> None:
    with pytest.raises(ValidationError):
        VisitIn(vertical="saude", token="")
    assert VisitIn(vertical="saude", token="tok_12345678").token == "tok_12345678"


def test_rate_limiter_window() -> None:
    rl = RateLimiter()
    assert all(rl.allow("k", 3, 60, now=0.0) for _ in range(3))
    assert not rl.allow("k", 3, 60, now=1.0)
    assert rl.allow("k", 3, 60, now=61.0)


def test_rate_limiter_prunes_expired_keys() -> None:
    rl = RateLimiter(max_keys=2)
    for i in range(3):
        rl.allow(f"k{i}", 1, 10, now=0.0)
    rl.allow("fresh", 1, 10, now=100.0)
    assert set(rl._buckets) == {"fresh"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(intake.settings, "landings_intake_secret", "s3cret")
    monkeypatch.setattr(intake, "limiter", RateLimiter())
    app = FastAPI()
    app.include_router(intake.router)
    return TestClient(app)


def test_missing_secret_is_401_before_validation(client: TestClient) -> None:
    r = client.post("/api/leads", json={"junk": True})
    assert r.status_code == 401


def test_wrong_secret_is_401(client: TestClient) -> None:
    r = client.post("/api/visits", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_intake_disabled_without_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intake.settings, "landings_intake_secret", "")
    r = client.post("/api/leads", json=LEAD, headers={"Authorization": "Bearer "})
    assert r.status_code == 503


def test_authorized_bad_body_is_422(client: TestClient) -> None:
    r = client.post(
        "/api/leads",
        json={**LEAD, "email": "nope"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 422


def test_per_visitor_rate_limit(client: TestClient) -> None:
    headers = {"Authorization": "Bearer s3cret", "X-Visitor-IP": "203.0.113.9"}
    codes = [
        client.post("/api/leads", json={**LEAD, "email": "bad"}, headers=headers).status_code
        for _ in range(6)
    ]
    # The limiter runs before body validation: five 422s, then 429.
    assert codes == [422] * 5 + [429]
