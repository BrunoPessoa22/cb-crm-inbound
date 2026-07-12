"""Inbound email classification.

Buckets every inbound message into exactly one category:

- ``bounce``  — SMTP delivery-status notification (mailer-daemon).
- ``auto``    — machine-generated auto-reply that is NOT an OOO
                (ticket ack, no-reply notifications, ...).
- ``ooo``     — out-of-office / vacation auto-reply (PT-BR + EN).
- ``optout``  — human asking to be removed.
- ``reply``   — a real human reply. The money case.

Detection order matters: header-based auto detection runs first because an
OOO auto-reply can contain arbitrary text; opt-out keywords only apply to
mail that is not machine-generated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from typing import Mapping

Category = str  # "bounce" | "auto" | "ooo" | "optout" | "reply"


@dataclass(frozen=True)
class Classification:
    category: Category
    reason: str


# --- header signals ---------------------------------------------------------

_AUTO_SUBMITTED_OK = {"", "no"}
_PRECEDENCE_AUTO = {"bulk", "junk", "auto_reply", "auto-reply", "list"}
_BOUNCE_SENDERS = re.compile(
    r"(mailer-daemon|postmaster)@", re.IGNORECASE
)

# --- OOO text signals (subject or body) -------------------------------------

_OOO_SUBJECT = re.compile(
    r"("
    r"resposta\s+autom[aá]tica|autom[aá]tic\s+reply|automatic\s+reply|"
    r"auto[\s-]?reply|out\s+of\s+(the\s+)?office|aus[eê]ncia|ausente|"
    r"f[eé]rias|licen[cç]a"
    r")",
    re.IGNORECASE,
)
_OOO_BODY = re.compile(
    r"("
    r"estou\s+ausente|estarei\s+ausente|estou\s+de\s+f[eé]rias|"
    r"estarei\s+de\s+f[eé]rias|em\s+f[eé]rias|fora\s+do\s+escrit[oó]rio|"
    r"estarei\s+fora|estou\s+fora\s+do|retorno\s+(no\s+dia|em|a partir)|"
    r"retornarei\s+(no\s+dia|em|a partir)|sem\s+acesso\s+ao\s+e-?mail|"
    r"out\s+of\s+(the\s+)?office|away\s+from\s+(the\s+)?office|"
    r"on\s+vacation|annual\s+leave|maternity\s+leave|licen[cç]a[\s-]"
    r"maternidade|responderei\s+(assim\s+que|quando)\s+(poss[ií]vel|retornar)"
    r")",
    re.IGNORECASE,
)

# --- opt-out signals ---------------------------------------------------------

_OPTOUT = re.compile(
    r"("
    r"\bremover\b|\bremova(m|r)?\s+(meu|o\s+meu|este)|\bdescadastr\w+|"
    r"n[aã]o\s+quero\s+(mais\s+)?receber|n[aã]o\s+tenho\s+interesse|"
    r"\bparar\s+de\s+(enviar|receber)|\bpare(m)?\s+de\s+(enviar|me\s+enviar)|"
    r"\bsair\s+da\s+lista|\bme\s+tire(m)?\s+da\s+lista|"
    r"\bunsubscribe\b|\bopt[\s-]?out\b|\bremove\s+me\b|\bstop\s+emailing\b"
    r")",
    re.IGNORECASE,
)


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return (value or "").strip()
    return ""


def headers_from_message(msg: Message) -> dict[str, str]:
    """Flatten a Message's headers into a case-preserving dict.

    Repeated headers keep the first occurrence, which is the relevant one
    for every signal we read (Auto-Submitted, Precedence, ...).
    """
    out: dict[str, str] = {}
    for key, value in msg.items():
        if key not in out:
            out[key] = str(value)
    return out


def classify(
    from_addr: str,
    subject: str,
    body_text: str,
    headers: Mapping[str, str],
) -> Classification:
    subject = subject or ""
    body = (body_text or "")[:20000]

    # 1. Delivery status notifications.
    if _BOUNCE_SENDERS.search(from_addr or ""):
        return Classification("bounce", "sender is mailer-daemon/postmaster")
    ct = _header(headers, "Content-Type").lower()
    if "multipart/report" in ct and "delivery-status" in ct:
        return Classification("bounce", "multipart/report delivery-status")

    # 2. Machine-generated mail (headers are authoritative).
    auto_submitted = _header(headers, "Auto-Submitted").lower()
    is_auto = bool(auto_submitted) and auto_submitted not in _AUTO_SUBMITTED_OK
    precedence = _header(headers, "Precedence").lower()
    if precedence in _PRECEDENCE_AUTO:
        is_auto = True
    if _header(headers, "X-Autoreply") or _header(headers, "X-Autorespond"):
        is_auto = True
    if _header(headers, "X-Auto-Response-Suppress"):
        is_auto = True
    if is_auto:
        if _OOO_SUBJECT.search(subject) or _OOO_BODY.search(body):
            return Classification("ooo", "auto-submitted with OOO text")
        return Classification("auto", "auto-submitted headers")

    # 3. Human opt-out.
    if _OPTOUT.search(subject) or _OPTOUT.search(body[:4000]):
        return Classification("optout", "opt-out keywords")

    # 4. OOO without proper headers (common with BR mail servers).
    if _OOO_SUBJECT.search(subject):
        return Classification("ooo", "OOO subject heuristics")
    if _OOO_BODY.search(body[:4000]):
        return Classification("ooo", "OOO body heuristics")

    # 5. A real human reply.
    return Classification("reply", "no auto/optout/ooo signals")
