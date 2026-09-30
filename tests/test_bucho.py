"""app/bucho.py（部長別分類・集計）の単体テスト。"""

from datetime import datetime, timezone

from app import bucho


class TestClassify:
    def test_project_map_dev(self):
        assert bucho.classify("knowhow", [], "") == "sanada"
        assert bucho.classify("seiko", ["請求書"], "") == "sanada"  # 明示マップが最優先

    def test_project_map_kujo(self):
        assert bucho.classify("monthly-cf", [], "") == "kujo"

    def test_project_map_kagura(self):
        # 2026-09-26新設・神楽（AI・DX担当 副社長）の社長代行/成長ループ。
        # 未登録だと全社共通に誤分類されるバグの回帰テスト。
        assert bucho.classify("aidx-shacho-daikou", [], "") == "kagura"

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

    _WINDOWS = [
        {"key": "d1", "since": "2026-06-18T12:00:00+00:00"},
        {"key": "d7", "since": "2026-06-12T12:00:00+00:00"},
        {"key": "d30", "since": "2026-05-20T12:00:00+00:00"},
    ]

    def test_windows_counts(self):
        out = bucho.aggregate_compare(self._rows(), self._WINDOWS)
        assert out["sanada"]["d1"] == 1 and out["sanada"]["d7"] == 2 and out["sanada"]["d30"] == 3
        assert out["kujo"]["d1"] == 1 and out["kujo"]["d7"] == 1 and out["kujo"]["d30"] == 1
        assert out["common"]["d1"] == 0 and out["common"]["d7"] == 0 and out["common"]["d30"] == 0

    def test_all_keys_present(self):
        out = bucho.aggregate_compare([], self._WINDOWS)
        assert set(out.keys()) == set(bucho.BUCHO_KEYS)


class TestGrowthRatio:
    def test_basic(self):
        # 期間前の総数100に対して+25 → +25%
        assert bucho.growth_ratio(25, 125) == 25.0

    def test_eightfold(self):
        # 期間前776、今6191増えた（総数6967）→ 約+797.8%（約8倍）
        assert bucho.growth_ratio(6191, 6967) == 797.8

    def test_none_when_all_new(self):
        # 全部その期間内に増えた（期間前ゼロ）→ 新規（比較対象なし）
        assert bucho.growth_ratio(483, 483) is None

    def test_zero_added(self):
        assert bucho.growth_ratio(0, 100) == 0.0


class TestNarrative:
    def test_no_data_yet(self):
        assert bucho.narrative({"total": 0, "added": 0, "growth_pct": None, "recalls": 0, "daily": []}) \
            == "まだ知恵がありません（これから）。"

    def test_pace_dropped_uses_last_nonzero_day_not_trailing_zero(self):
        # 本日分(最後の要素)がまだ0件でも、それだけで「ペースが落ちた」としない
        d = {
            "total": 10, "added": 3, "growth_pct": 42.9, "recalls": 5,
            "daily": [
                {"period": "2026-09-28", "added": 1},
                {"period": "2026-09-29", "added": 2},
                {"period": "2026-09-30", "added": 0},  # 未確定の0件
            ],
        }
        text = bucho.narrative(d)
        assert "ペースは落ちていません" in text
        assert "要確認" not in text

    def test_pace_actually_dropped(self):
        d = {
            "total": 20, "added": 3, "growth_pct": 10.0, "recalls": 5,
            "daily": [
                {"period": "2026-09-27", "added": 15},
                {"period": "2026-09-28", "added": 8},
                {"period": "2026-09-29", "added": 2},
            ],
        }
        text = bucho.narrative(d)
        assert "要確認" in text
        assert "-86.7%" in text

    def test_no_recalls_yet(self):
        d = {"total": 1, "added": 1, "growth_pct": None, "recalls": 0, "daily": [{"period": "2026-09-30", "added": 1}]}
        assert "まだ実際に使われた記録（recall）はありません" in bucho.narrative(d)

    def test_three_consecutive_zero_days_is_flagged_not_silently_ok(self):
        # 2026-09-30バグチェック回帰: 旧実装は0件日を全て除外していたため、
        # 直近3日連続で活動ゼロでも(その前にpeakがあれば)「落ちていません」と誤表示しえた。
        # 末尾1日(本日)は「未確定の0件」として除外されるため、確定した3連続ゼロを見せるには
        # 末尾に4件目の0件(=本日分)を足す。
        d = {
            "total": 10, "added": 3, "growth_pct": 10.0, "recalls": 5,
            "daily": [
                {"period": "2026-09-25", "added": 3},
                {"period": "2026-09-26", "added": 0},
                {"period": "2026-09-27", "added": 0},
                {"period": "2026-09-28", "added": 0},
                {"period": "2026-09-29", "added": 0},  # 本日分(未確定として除外される)
            ],
        }
        text = bucho.narrative(d)
        assert "直近3日、知恵の追加がありません" in text
        assert "落ちていません" not in text

    def test_mid_zone_decline_is_not_silent(self):
        # peakの50〜100%未満への低下は、旧実装ではどの分岐にも当たらず無言になっていた
        d = {
            "total": 16, "added": 6, "growth_pct": 20.0, "recalls": 3,
            "daily": [
                {"period": "2026-09-28", "added": 10},
                {"period": "2026-09-29", "added": 7},
            ],
        }
        text = bucho.narrative(d)
        assert "下がっていますが大きな落ち込みではありません" in text

    def test_peak_zero_says_nothing_about_pace(self):
        d = {
            "total": 0, "added": 0, "growth_pct": None, "recalls": 0,
            "daily": [{"period": "2026-09-29", "added": 0}, {"period": "2026-09-30", "added": 0}],
        }
        # total=0で早期returnするため、これは「total>0だがdailyが両方0」の別ケースで検証
        d["total"] = 1
        text = bucho.narrative(d)
        assert "ペース" not in text


class TestBuchoDefsRank:
    def test_kagura_is_svp_and_first(self):
        assert bucho.BUCHO_DEFS[0]["key"] == "kagura"
        assert bucho.BUCHO_DEFS[0]["rank"] == "svp"
        assert bucho.BUCHO_DEFS[0]["title"] == "AI・DX担当 副社長（CAIO/CDXO）"

    def test_all_others_are_bucho_rank(self):
        for d in bucho.BUCHO_DEFS[1:]:
            assert d["rank"] == "bucho"


class TestDayLabels:
    def test_ten_days_oldest_first(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        days = bucho.day_labels(now, 10)
        assert days[0] == "2026-09-21"
        assert days[-1] == "2026-09-30"
        assert len(days) == 10

    def test_month_boundary(self):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        days = bucho.day_labels(now, 5)
        assert days == ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]

    def test_jst_boundary_utc_evening_is_next_day_jst(self):
        # 2026-09-30バグチェック回帰: UTC 22:00は既にJSTでは翌日9:00。
        # 旧実装はUTCのまま日付を切っていたため、この時刻を「今日」とすると
        # 実際はJSTで翌日扱いになるべきところが前日のまま出ていた。
        now_utc_evening = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)  # JST: 2026-09-30 07:00
        days = bucho.day_labels(now_utc_evening, 3)
        assert days[-1] == "2026-09-30"


class TestMonthLabels:
    def test_six_months(self):
        assert bucho.month_labels("2026-06") == [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"
        ]

    def test_year_boundary(self):
        assert bucho.month_labels("2026-02", n=4) == ["2025-11", "2025-12", "2026-01", "2026-02"]


class TestDailyTimezoneBoundary:
    """narrative()/detail()のJST・UTC日付境界ズレ再現テスト（bug-check-lab 堀内逆検証確定・🟠High）。

    day_labels(now, n) と detail() 内のdaily集計は、created_atのUTC文字列先頭10文字を
    そのまま日付バケットのキーにしている。now も datetime.now(timezone.utc) をそのまま
    使っているため、JSTで「今日」の活動が UTC基準では「前日」に計上されてしまう。

    再現条件:
      now_utc = 2026-09-30T00:30Z （JSTでは2026-09-30 09:30＝まだ「今日」の朝）
      created_at = 2026-09-29T16:00:00+00:00
        → UTC視点では9/29だが、+9hしたJST時刻は2026-09-30 01:00＝JSTでは「今日」9/30未明の活動。

    JST基準で正しく集計するなら、この1件は day_labels の最終日（2026-09-30）の
    バケットに入るべきだが、現状の実装は created_at 文字列の先頭10文字（"2026-09-29"）を
    そのまま使うため前日のバケットに計上してしまう。
    """

    def test_jst_today_activity_is_not_miscounted_as_yesterday(self):
        now_utc = datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc)
        rows = [
            {"chunk_id": 1, "project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-09-29T16:00:00+00:00", "recall_count": 0},
        ]
        d = bucho.detail(
            rows, "kujo",
            "2026-09-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", "2026-09",
            now=now_utc, daily_n=10,
        )
        # day_labels の最終日は「今日」= 2026-09-30 のはず
        assert d["daily"][-1]["period"] == "2026-09-30"
        # このデータはJST視点では2026-09-30 01:00の活動＝「今日」に計上されるべき。
        # 現状の実装ではUTC文字列先頭10文字("2026-09-29")のバケットに入ってしまい、
        # "2026-09-30"バケットは0件のまま＝JSTでは今日の活動が前日扱いになるバグの証拠。
        assert d["daily"][-1]["added"] == 1, (
            "JSTでは今日(2026-09-30)の活動のはずが、UTC日付境界のまま集計しているため"
            "前日(2026-09-29)のバケットに計上されてしまっている（JST/UTC境界ズレのバグ再現）"
        )


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
        # {**d, **result}マージ後もBUCHO_DEFS由来のフィールドが保持されていること（回帰確認）
        assert d["key"] == "kujo"
        assert d["rank"] == "bucho"
        assert d["name"] == "九条 玲"
        assert "narrative" in d

    def test_sanada_excludes_others(self):
        d = bucho.detail(
            self._rows(), "sanada",
            "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "2026-06",
        )
        assert d["total"] == 1
        assert d["recent_items"][0]["project_key"] == "knowhow"

    def test_daily_without_now_is_empty(self):
        d = bucho.detail(
            self._rows(), "kujo",
            "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "2026-06",
        )
        assert d["daily"] == []

    def test_daily_with_now_counts_per_day(self):
        rows = [
            {"chunk_id": 1, "project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-06-10T03:00:00+00:00", "recall_count": 0},
            {"chunk_id": 2, "project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-06-10T09:00:00+00:00", "recall_count": 0},
            {"chunk_id": 3, "project_key": "monthly-cf", "tags": [], "content_head": "",
             "created_at": "2026-05-20T00:00:00+00:00", "recall_count": 0},  # 範囲外(10日より前)
        ]
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        d = bucho.detail(
            rows, "kujo",
            "2026-05-31T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "2026-06",
            now=now, daily_n=10,
        )
        assert len(d["daily"]) == 10
        assert d["daily"][-1] == {"period": "2026-06-10", "added": 2}
        assert sum(x["added"] for x in d["daily"]) == 2
