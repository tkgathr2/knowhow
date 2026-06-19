"""コストカッターα 純粋ロジックの単体テスト（DB非依存）。"""

from app import cost_cutter as cc


def test_pick_baseline_picks_peak_completed_month():
    monthly = [
        {"month": "2026-01", "total_jpy": 1_000_000},
        {"month": "2026-02", "total_jpy": 3_000_000},  # ピーク
        {"month": "2026-03", "total_jpy": 2_000_000},
        {"month": "2026-04", "total_jpy": 500_000},  # 当月（未完了）
    ]
    base = cc.pick_baseline(monthly, current_month="2026-04")
    assert base is not None
    assert base["month"] == "2026-02"
    assert base["total_jpy"] == 3_000_000


def test_pick_baseline_excludes_current_even_if_largest():
    monthly = [
        {"month": "2026-03", "total_jpy": 2_000_000},
        {"month": "2026-04", "total_jpy": 9_000_000},  # 当月だが除外対象
    ]
    base = cc.pick_baseline(monthly, current_month="2026-04")
    assert base is not None
    assert base["month"] == "2026-03"


def test_pick_baseline_none_when_no_completed_history():
    monthly = [
        {"month": "2026-03", "total_jpy": 0},
        {"month": "2026-04", "total_jpy": 500_000},  # 当月のみ
    ]
    assert cc.pick_baseline(monthly, current_month="2026-04") is None


def test_reduction_basic():
    r = cc.reduction(baseline_jpy=3_000_000, projection_jpy=600_000)
    assert r["reduction_jpy"] == 2_400_000
    assert r["reduction_pct"] == 80.0


def test_reduction_no_negative_when_increased():
    r = cc.reduction(baseline_jpy=1_000_000, projection_jpy=1_500_000)
    assert r["reduction_jpy"] == 0
    assert r["reduction_pct"] == 0.0


def test_reduction_zero_baseline():
    r = cc.reduction(baseline_jpy=0, projection_jpy=500_000)
    assert r["reduction_jpy"] == 0
    assert r["reduction_pct"] == 0.0


def test_annualized_saving():
    assert cc.annualized_saving(3_000_000, 600_000) == 2_400_000 * 12
    assert cc.annualized_saving(1_000_000, 1_500_000) == 0


def test_assemble_breakdown_fixed_order_and_zero_fill():
    out = cc.assemble_breakdown({"extra_usage": 1_200_000, "subscription": 30_000})
    kinds = [b["kind"] for b in out]
    assert kinds == ["subscription", "extra_usage", "api_credit", "other"]
    by = {b["kind"]: b["jpy"] for b in out}
    assert by["subscription"] == 30_000
    assert by["extra_usage"] == 1_200_000
    assert by["api_credit"] == 0  # 0埋め
    # ラベルが付く
    assert out[0]["label"] == "Maxプラン月額"


def test_overage_excludes_subscription():
    by_kind = {
        "subscription": 30_000,
        "extra_usage": 1_200_000,
        "api_credit": 50_000,
        "other": 1_000,
    }
    # ムダ＝subscription以外の合計
    assert cc.overage_jpy(by_kind) == 1_200_000 + 50_000 + 1_000


def test_overage_zero_when_only_subscription():
    assert cc.overage_jpy({"subscription": 30_000}) == 0
