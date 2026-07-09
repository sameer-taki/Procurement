"""M365 Graph mailer — client-credentials sendMail as no-reply@golden.com.fj.
Uses the existing 'Golden Apps Mailer' app registration (Mail.Send, scoped via
an Exchange application access policy). The client-credentials token is cached
until shortly before expiry — one token round-trip per hour, not per email."""
import logging
import re
import threading
import time

import httpx

from .config import settings

log = logging.getLogger("golden.procurement.mailer")

# Refresh this many seconds BEFORE the token's stated expiry, so a token that is
# about to lapse is never handed to sendMail.
_TOKEN_SKEW_S = 120
_token_lock = threading.Lock()
_token_cache: dict = {"value": None, "expires_at": 0.0}

# Conservative single-address validation: no whitespace/control chars, exactly one
# '@', a non-empty local part, and a dotted domain. We are not trying to be RFC-5322
# complete — just to keep a malformed/empty/header-injecting value in the vendor
# master from ever reaching Graph sendMail.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _valid_email(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    if not addr or any(ord(c) < 32 for c in addr):
        return False
    return bool(_EMAIL_RE.match(addr))


def _fetch_token() -> tuple:
    """(access_token, expires_in_seconds) from the Entra token endpoint."""
    r = httpx.post(
        f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": settings.graph_client_id,
            "client_secret": settings.graph_client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    return body["access_token"], float(body.get("expires_in") or 0)


def _token() -> str:
    """Cached client-credentials token, refreshed _TOKEN_SKEW_S before expiry.
    Lock held across the fetch so concurrent senders (request thread + outbox
    scheduler) share one refresh instead of racing the token endpoint. A failed
    refresh raises to the caller (notify() already contains mail errors)."""
    now = time.monotonic()
    with _token_lock:
        if _token_cache["value"] and now < _token_cache["expires_at"]:
            return _token_cache["value"]
        value, expires_in = _fetch_token()
        # A token with no/short expiry is used once but never cached.
        _token_cache["value"] = value
        _token_cache["expires_at"] = (
            now + expires_in - _TOKEN_SKEW_S if expires_in > _TOKEN_SKEW_S else 0.0
        )
        return value


def send_mail(to: list[str], subject: str, html: str) -> None:
    token = _token()
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": False,
    }
    r = httpx.post(
        f"https://graph.microsoft.com/v1.0/users/{settings.graph_sender}/sendMail",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def notify(to: list[str], subject: str, html: str) -> str:
    """Guarded vendor-notify path used by the PO flow.

    Never raises and never lets an email problem break the surrounding PO
    transition. Returns a status string the caller records as an OrderEvent:
      - 'skipped:not-configured'  Graph not configured (no tenant/client/secret)
      - 'skipped:no-recipient'    nothing to send to
      - 'skipped:invalid-recipient'  recipient failed basic address validation
      - 'sent'                    sendMail succeeded
      - 'error:<msg>'             sendMail failed (logged; flow continues)
    """
    if not settings.graph_enabled:
        log.info("vendor notify skipped: Graph not configured (subject=%r)", subject)
        return "skipped:not-configured"
    candidates = [a for a in (to or []) if a and str(a).strip()]
    if not candidates:
        log.info("vendor notify skipped: no recipient (subject=%r)", subject)
        return "skipped:no-recipient"
    recipients = [a.strip() for a in candidates if _valid_email(a)]
    if not recipients:
        log.warning("vendor notify skipped: invalid recipient(s) (subject=%r)", subject)
        return "skipped:invalid-recipient"
    try:
        send_mail(recipients, subject, html)
        return "sent"
    except Exception as exc:  # never break the PO flow on a mail failure
        log.exception("vendor notify failed (subject=%r)", subject)
        return f"error:{exc}"
