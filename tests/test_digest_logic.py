"""app/digest_logic.py（1日のダイジェスト純粋ロジック）の単体テスト。"""

from app import digest_logic


def _items():
    return [
        {"project_key": "seiko", "tags": ["請求書", "auto-ingest"], "content": "請求書の自動取込を直した。"},
        {"project_key": "seiko", "tags": ["請求書"], "content": "MF連携の権限を整理した。"},
        {"project_key": "knowhow", "tags": ["ダッシュボード"], "content": "成長画面を見やすくした。"},
    ]


def _stats(**kw):
    base = {"asset_added": 3, "log_added": 5, "recalled": 7, "deprecated": 1}
    base.update(kw)
    return base


class TestBuildLlmInput:
    def test_contains_counts_and_items(self):
        text = digest_logic.build_llm_input("2026-06-12", _stats(), _items())
        assert "2026-06-12" in text
        assert "3件" in text
        assert "[seiko]" in text
        assert "請求書の自動取込" in text

    def test_caps_items(self):
        many = [{"project_key": "p", "tags": [], "content": f"item{i}"} for i in range(100)]
        text = digest_logic.build_llm_input("2026-06-12", _stats(), many)
        assert f"ほか {100 - digest_logic.MAX_ITEMS_FOR_LLM} 件" in text


class TestFallbackDigest:
    def test_quiet_day(self):
        d = digest_logic.fallback_digest("2026-06-12", _stats(asset_added=0, recalled=0), [])
        assert d["headline"] == "静かな1日"
        assert "2026-06-12" in d["body"]

    def test_normal_day_mentions_counts_and_topics(self):
        d = digest_logic.fallback_digest("2026-06-12", _stats(), _items())
        assert "3件" in d["body"]
        assert "請求書" in d["body"]      # 頻出タグがテーマとして出る
        assert "seiko" in d["body"]       # 一番学びが多い仕事
        assert "7件" in d["body"]         # 使われた知識
        assert d["headline"]

    def test_skips_noise_tags(self):
        items = [{"project_key": "p", "tags": ["auto-ingest", "学び"], "content": "x"}]
        assert digest_logic.top_topics(items) == []


class TestNormalizeLlmDigest:
    def test_valid_passthrough(self):
        out = digest_logic.normalize_llm_digest(
            {"headline": "良い日", "body": "本文です。"}, "2026-06-12", _stats(), _items()
        )
        assert out == {"headline": "良い日", "body": "本文です。"}

    def test_none_falls_back(self):
        out = digest_logic.normalize_llm_digest(None, "2026-06-12", _stats(), _items())
        assert out["headline"]
        assert out["body"]

    def test_missing_body_falls_back(self):
        out = digest_logic.normalize_llm_digest({"headline": "x", "body": ""}, "2026-06-12", _stats(), _items())
        assert "3件" in out["body"]

    def test_truncates_long_output(self):
        out = digest_logic.normalize_llm_digest(
            {"headline": "あ" * 100, "body": "い" * 5000}, "2026-06-12", _stats(), _items()
        )
        assert len(out["headline"]) <= 40
        assert len(out["body"]) <= 2000
