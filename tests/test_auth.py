"""Google ログインの純粋ロジック（session 署名 / oauth 許可判定）の単体テスト。"""

from app import oauth
from app import session as sess

SECRET = "test-secret-key-0123456789"


def test_session_roundtrip():
    token = sess.make_session("a@takagi.bz", SECRET, issued_ts=1000, max_age_s=180 * 86400)
    payload = sess.verify(token, SECRET, now_ts=1000 + 86400)  # 1日後はまだ有効
    assert payload is not None
    assert payload["email"] == "a@takagi.bz"
    assert payload["exp"] == 1000 + 180 * 86400


def test_session_expired():
    token = sess.make_session("a@takagi.bz", SECRET, issued_ts=1000, max_age_s=100)
    assert sess.verify(token, SECRET, now_ts=1101) is None  # 期限切れ


def test_session_tampered_signature():
    token = sess.make_session("a@takagi.bz", SECRET, issued_ts=1000, max_age_s=100)
    body, _, sig = token.partition(".")
    forged = body + "." + ("A" * len(sig))
    assert sess.verify(forged, SECRET, now_ts=1001) is None


def test_session_wrong_secret():
    token = sess.make_session("a@takagi.bz", SECRET, issued_ts=1000, max_age_s=100)
    assert sess.verify(token, "other-secret", now_ts=1001) is None


def test_session_garbage():
    assert sess.verify(None, SECRET, 1) is None
    assert sess.verify("", SECRET, 1) is None
    assert sess.verify("no-dot", SECRET, 1) is None


def test_email_allowed_by_domain():
    assert oauth.email_allowed("ceo@takagi.bz", "takagi.bz,positive-z.co.jp", "") is True
    assert oauth.email_allowed("X@Positive-Z.co.jp", "takagi.bz,positive-z.co.jp", "") is True
    assert oauth.email_allowed("intruder@gmail.com", "takagi.bz", "") is False


def test_email_allowed_by_explicit_email():
    assert oauth.email_allowed("vendor@gmail.com", "", "vendor@gmail.com") is True


def test_email_allowed_denies_when_unconfigured():
    assert oauth.email_allowed("anyone@gmail.com", "", "") is False
    assert oauth.email_allowed(None, "takagi.bz", "") is False


def test_authorize_url_has_params():
    url = oauth.authorize_url("CID", "https://x.app/auth/callback", "STATE123")
    assert url.startswith(oauth.GOOGLE_AUTH_ENDPOINT)
    assert "client_id=CID" in url
    assert "response_type=code" in url
    assert "state=STATE123" in url
    assert "scope=openid+email+profile" in url


def test_redirect_uri_for():
    assert oauth.redirect_uri_for("https://x.app/") == "https://x.app/auth/callback"
    assert oauth.redirect_uri_for("https://x.app") == "https://x.app/auth/callback"


def test_safe_next_blocks_open_redirect():
    assert oauth.safe_next("/growth") == "/growth"
    assert oauth.safe_next("https://evil.com") == "/"
    assert oauth.safe_next("//evil.com") == "/"
    assert oauth.safe_next(None) == "/"
