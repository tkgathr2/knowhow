"""app/bucho.py（部長別分類・集計）の単体テスト。"""

from app import bucho


class TestClassify:
    def test_project_map_dev(self):
        assert bucho.classify("knowhow", [], "") == "sanada"
        assert bucho.classify("seiko", ["請求書"], "") == "sanada"  # 明示マップが最優先

    def test_project_map_kujo(self):
        assert bucho.classify("monthly-cf", [], "") == "kujo"

    def test_keyword_kujo(self):
        assert bucho.classify("cto-lab", ["資金繰り"], "") == "kujo"
        assert bucho.classify("cto-lab", [], "MFクラウドの試算表を確認") == "kujo"

    def test_keyword_kirishima(self):
        assert bucho.classify("cto-lab", ["契約"], "") == "kirishima"

    def test_keyword_todo(self):
        assert bucho.classify("cto-lab", ["採用"], "") == "todo"

    def test_keyword_muroi(self):
        assert bucho.classify("cto-lab", ["勤怠"], "") == "muroi"

    def test_default_cto_lab_is_sanada(self):
        assert bucho.classify("cto-lab", ["その他"], "特に該当なし") == "sanada"

    def test_unknown_project_is_common(self):
        assert bucho.classify("brand-new-project", [], "") == "common"

    def test_empty_inputs(self):
        assert bucho.classify("", None, "") == "common"


class TestAggregate:
    def _rows(self):
        return [
            {"project_key": "knowhow", "tags": [], "content_head": "",
             "created_at": "2026-06-12T00:00:00+00:00", "recall_count": 5},
            {"project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-05-20T00:00:00+00:00", "recall_count": 2},
            {"project_key": "cto-lab", "tags": ["契約"], "content_head": "",
             "created_at": "2026-06-01T00:00:00+00:00", "recall_count": 0},
        ]

    def test_counts(self):
        out = bucho.aggregate(self._rows(), "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        m = {b["key"]: b for b in out}
        assert m["sanada"]["total"] == 1 and m["sanada"]["added"] == 1
        assert m["kujo"]["total"] == 1 and m["kujo"]["added"] == 0 and m["kujo"]["added_prev"] == 1
        assert m["kirishima"]["total"] == 1 and m["kirishima"]["added"] == 1
        assert m["sanada"]["recalls"] == 5

    def test_all_buchos_present(self):
        out = bucho.aggregate([], "2026-06-01", "2026-05-01")
        assert [b["key"] for b in out] == bucho.BUCHO_KEYS
        assert all(b["total"] == 0 for b in out)

    def test_growth_pct(self):
        rows = [
            {"project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-06-10T00:00:00+00:00", "recall_count": 0},
            {"project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-06-11T00:00:00+00:00", "recall_count": 0},
            {"project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-05-15T00:00:00+00:00", "recall_count": 0},
        ]
        out = bucho.aggregate(rows, "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        kujo = next(b for b in out if b["key"] == "kujo")
        assert kujo["added"] == 2 and kujo["added_prev"] == 1
        assert kujo["growth_pct"] == 100.0

    def test_growth_pct_none_when_no_prev(self):
        rows = [{"project_key": "monthly-cf", "tags": [], "content_head": "",
                 "created_at": "2026-06-10T00:00:00+00:00", "recall_count": 0}]
        out = bucho.aggregate(rows, "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        kujo = next(b for b in out if b["key"] == "kujo")
        assert kujo["growth_pct"] is None
