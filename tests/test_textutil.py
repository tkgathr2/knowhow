"""app/textutil.py（UTF-8健全化）と修復ヘルパーの単体テスト。"""

from app.textutil import sanitize_tags, sanitize_utf8
from app.routers.intelligence import _repair_bytes


class TestSanitizeUtf8:
    def test_normal_passthrough(self):
        assert sanitize_utf8("こんにちは") == "こんにちは"

    def test_empty_and_none(self):
        assert sanitize_utf8("") == ""
        assert sanitize_utf8(None) == ""

    def test_lone_surrogate_replaced(self):
        broken = "abc" + "\udce3" + "def"  # surrogateescape 由来の孤立サロゲート
        out = sanitize_utf8(broken)
        out.encode("utf-8")  # 保存可能になっている
        assert "abc" in out and "def" in out

    def test_tags(self):
        assert sanitize_tags(None) == []
        out = sanitize_tags(["正常", "x\udce3y"])
        for t in out:
            t.encode("utf-8")


class TestRepairBytes:
    def test_valid_utf8(self):
        assert _repair_bytes("学び".encode("utf-8")) == "学び"

    def test_cp932_restored(self):
        # cp932 のバイト列は文字化け復元される
        assert _repair_bytes("学び".encode("cp932")) == "学び"

    def test_truncated_utf8_tail(self):
        b = "カタカナ".encode("utf-8")[:-1]  # 末尾1バイト欠け
        out = _repair_bytes(b)
        out.encode("utf-8")
        assert out.startswith("カタカ")

    def test_garbage_never_raises(self):
        out = _repair_bytes(bytes(range(256)))
        out.encode("utf-8")
