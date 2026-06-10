"""app.growth（成長計算ロジック）の単体テスト。DB非依存。"""

from datetime import datetime, timezone

from app import growth


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_period_label_month():
    assert growth.period_label(_dt(2026, 6, 9), "month") == "2026-06"
    assert growth.period_label(_dt(2026, 12, 31), "month") == "2026-12"


def test_period_label_week_iso():
    # 2026-06-08 は ISO 2026 年第24週
    assert growth.period_label(_dt(2026, 6, 8), "week") == "2026-W24"


def test_days_in_period():
    assert growth.days_in_period("2026-02", "month") == 28
    assert growth.days_in_period("2024-02", "month") == 29  # 閏年
    assert growth.days_in_period("2026-06", "month") == 30
    assert growth.days_in_period("2026-W24", "week") == 7


def test_build_points_cumulative_and_growth():
    added = {"2026-02": 476, "2026-03": 41, "2026-04": 152}
    points = growth.build_points(added, {})
    assert [p["period"] for p in points] == ["2026-02", "2026-03", "2026-04"]
    assert [p["cumulative"] for p in points] == [476, 517, 669]
    # 最初の期は前期末=0 なので成長率は None（新規）
    assert points[0]["growth_pct"] is None
    # 3月: 41 / 476 = 8.6%
    assert points[1]["growth_pct"] == 8.6
    # 4月: 152 / 517 = 29.4%
    assert points[2]["growth_pct"] == 29.4


def test_build_points_empty():
    assert growth.build_points({}, {}) == []


def test_build_points_deprecated_merge():
    added = {"2026-05": 10}
    points = growth.build_points(added, {"2026-05": 3})
    assert points[0]["deprecated_added"] == 3


def test_project_current_partial_month():
    added = {"2026-05": 100, "2026-06": 30}
    points = growth.build_points(added, {})
    # 6/10 時点（10日経過 / 30日）→ 着地 30/10*30 = 90
    cur = growth.project_current(points, _dt(2026, 6, 10), "month")
    assert cur is not None
    assert cur["period"] == "2026-06"
    assert cur["added_so_far"] == 30
    assert cur["days_elapsed"] == 10
    assert cur["days_in_period"] == 30
    assert cur["projected_added"] == 90
    # 着地90 / 前期末累計100 = 90.0%
    assert cur["projected_growth_pct"] == 90.0


def test_project_current_returns_none_when_latest_not_current():
    added = {"2026-04": 50, "2026-05": 20}
    points = growth.build_points(added, {})
    # 「今」は6月だが最新データは5月 → 予測しない
    assert growth.project_current(points, _dt(2026, 6, 10), "month") is None


def test_project_current_empty():
    assert growth.project_current([], _dt(2026, 6, 10), "month") is None


def test_make_narrative_accel_vs_decel():
    added = {"2026-04": 100, "2026-05": 50, "2026-06": 30}
    points = growth.build_points(added, {})
    cur = growth.project_current(points, _dt(2026, 6, 10), "month")  # 着地90
    text = growth.make_narrative(points, cur)
    assert "2026-06" in text
    assert "+30件" in text
    # 着地90 > 前期50 → 加速
    assert "加速" in text


def test_make_narrative_empty():
    assert growth.make_narrative([], None) == "まだ成長データがありません。"


def test_helpful_rate():
    assert growth.helpful_rate(0, 0) is None
    assert growth.helpful_rate(3, 1) == 75.0
    assert growth.helpful_rate(1, 0) == 100.0


def test_vectorized_pct():
    assert growth.vectorized_pct(1735, 1735) == 100.0
    assert growth.vectorized_pct(0, 0) == 0.0
    assert growth.vectorized_pct(1, 3) == 33.3
