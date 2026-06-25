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
