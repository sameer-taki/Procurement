"""Phase 3 hardening — mailer recipient validation (SECURITY lens).

mailer.notify() must never hand a malformed/empty/control-char address to Graph
sendMail. These tests assert the guard rejects bad recipients before any send and
that a valid recipient reaches send_mail.
"""
import app.mailer as mailer


def _enable_graph(monkeypatch):
    # Make graph_enabled True without real creds so notify() reaches validation.
    monkeypatch.setattr(mailer.settings, "graph_tenant_id", "t", raising=False)
    monkeypatch.setattr(mailer.settings, "graph_client_id", "c", raising=False)
    monkeypatch.setattr(mailer.settings, "graph_client_secret", "s", raising=False)


def test_valid_email_accepts_and_rejects():
    assert mailer._valid_email("sales@pacificpaper.com.fj")
    assert not mailer._valid_email("")
    assert not mailer._valid_email("   ")
    assert not mailer._valid_email("not-an-email")
    assert not mailer._valid_email("a@b")               # no dotted domain
    assert not mailer._valid_email("a b@c.com")          # whitespace
    assert not mailer._valid_email("a@c.com\r\nBcc: x@y.com")  # header injection
    assert not mailer._valid_email(None)


def test_notify_skips_invalid_recipient(monkeypatch):
    _enable_graph(monkeypatch)
    sent = {"n": 0}

    def _no_send(*a, **k):
        sent["n"] += 1

    monkeypatch.setattr(mailer, "send_mail", _no_send)
    assert mailer.notify(["garbage"], "S", "<p>x</p>") == "skipped:invalid-recipient"
    assert mailer.notify(["a@b.com\r\nBcc: evil@x.com"], "S", "<p>x</p>") \
        == "skipped:invalid-recipient"
    assert sent["n"] == 0                                # never reached Graph


def test_notify_sends_to_valid_recipient(monkeypatch):
    _enable_graph(monkeypatch)
    captured = {}

    def _capture(to, subject, html):
        captured["to"] = to

    monkeypatch.setattr(mailer, "send_mail", _capture)
    assert mailer.notify(["  sales@pacific.com.fj  "], "S", "<p>x</p>") == "sent"
    assert captured["to"] == ["sales@pacific.com.fj"]    # trimmed


def test_token_is_cached_until_near_expiry(monkeypatch):
    """One token fetch is reused across sends until the skew window before
    expiry; a short-lived token is never cached."""
    mailer._token_cache["value"] = None
    mailer._token_cache["expires_at"] = 0.0
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return f"tok-{calls['n']}", 3600.0        # 1h token

    monkeypatch.setattr(mailer, "_fetch_token", _fetch)
    assert mailer._token() == "tok-1"
    assert mailer._token() == "tok-1"             # served from cache
    assert calls["n"] == 1

    # Force the cache stale -> exactly one refresh.
    mailer._token_cache["expires_at"] = 0.0
    assert mailer._token() == "tok-2"
    assert calls["n"] == 2


def test_short_lived_token_is_not_cached(monkeypatch):
    mailer._token_cache["value"] = None
    mailer._token_cache["expires_at"] = 0.0
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return f"tok-{calls['n']}", 30.0          # shorter than the skew window

    monkeypatch.setattr(mailer, "_fetch_token", _fetch)
    mailer._token()
    mailer._token()
    assert calls["n"] == 2                         # never cached, refetched each call
