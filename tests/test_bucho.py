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


class TestAggregateCompare:
    def _rows(self):
        # now を 2026-06-19T12:00 と仮定した比較窓のテスト
        return [
            # 昨日(1日内) のもの → d1,d7,d30 すべて加算
            {"project_key": "knowhow", "tags": [], "content_head": "",
             "created_at": "2026-06-19T06:00:00+00:00", "recall_count": 0},
            # 直近1週間（だが昨日より前）→ d7,d30 のみ
            {"project_key": "knowhow", "tags": [], "content_head": "",
             "created_at": "2026-06-15T00:00:00+00:00", "recall_count": 0},
            # 直近1か月（だが1週間より前）→ d30 のみ
            {"project_key": "knowhow", "tags": [], "content_head": "",
             "created_at": "2026-06-01T00:00:00+00:00", "recall_count": 0},
            # 1か月より前 → どれにも入らない
            {"project_key": "knowhow", "tags": [], "content_head": "",
             "created_at": "2026-04-01T00:00:00+00:00", "recall_count": 0},
            # 別部長（kujo）昨日分
            {"project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-06-19T09:00:00+00:00", "recall_count": 0},
        ]

    def test_windows(self):
        out = bucho.aggregate_compare(
            self._rows(),
            "2026-06-18T12:00:00+00:00",  # 昨日(1日)
            "2026-06-12T12:00:00+00:00",  # 1週間
            "2026-05-20T12:00:00+00:00",  # 1か月
        )
        assert out["sanada"] == {"d1": 1, "d7": 2, "d30": 3}
        assert out["kujo"] == {"d1": 1, "d7": 1, "d30": 1}
        assert out["common"] == {"d1": 0, "d7": 0, "d30": 0}

    def test_all_keys_present(self):
        out = bucho.aggregate_compare([], "2026-06-18", "2026-06-12", "2026-05-20")
        assert set(out.keys()) == set(bucho.BUCHO_KEYS)


class TestMonthLabels:
    def test_six_months(self):
        assert bucho.month_labels("2026-06") == [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"
        ]

    def test_year_boundary(self):
        assert bucho.month_labels("2026-02", n=4) == ["2025-11", "2025-12", "2026-01", "2026-02"]


class TestDetail:
    def _rows(self):
        return [
            {"chunk_id": 1, "project_key": "monthly-cf", "tags": [], "content_head": "資金繰り表",
             "created_at": "2026-06-10T00:00:00+00:00", "recall_count": 4},
            {"chunk_id": 2, "project_key": "monthly-cf", "tags": [], "content_head": "返済予定",
             "created_at": "2026-05-15T00:00:00+00:00", "recall_count": 0},
            {"chunk_id": 3, "project_key": "cto-lab", "tags": ["経理"], "content_head": "仕訳の知見",
             "created_at": "2026-06-01T00:00:00+00:00", "recall_count": 2},
            {"chunk_id": 4, "project_key": "knowhow", "tags": [], "content_head": "開発の知見",
             "created_at": "2026-06-12T00:00:00+00:00", "recall_count": 9},
        ]

    def test_unknown_key_returns_none(self):
        assert bucho.detail([], "nobody", "2026-05-31", "2026-05-01", "2026-06") is None

    def test_kujo_detail(self):
        d = bucho.detail(
            self._rows(), "kujo",
            "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "2026-06",
        )
        assert d["total"] == 3                      # monthly-cf×2 + 経理タグのcto-lab
        assert d["added"] == 2 and d["added_prev"] == 1
        assert d["recalls"] == 6
        assert d["growth_pct"] == 100.0
        assert d["monthly"][-1]["period"] == "2026-06" and d["monthly"][-1]["added"] == 2
        assert d["recent_items"][0]["chunk_id"] == 1   # 新しい順
        assert d["top_recalled"][0]["chunk_id"] == 1   # recall 4 が最多
        assert d["top_projects"][0]["project_key"] == "monthly-cf"

    def test_sanada_excludes_others(self):
        d = bucho.detail(
            self._rows(), "sanada",
            "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "2026-06",
        )
        assert d["total"] == 1
        assert d["recent_items"][0]["project_key"] == "knowhow"
