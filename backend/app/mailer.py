"""M365 Graph mailer — client-credentials sendMail as no-reply@golden.com.fj.
Uses the existing 'Golden Apps Mailer' app registration (Mail.Send, scoped via
an Exchange application access policy). TODO: cache the token until expiry."""
import httpx

from .config import settings


def _token() -> str:
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
    return r.json()["access_token"]


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
