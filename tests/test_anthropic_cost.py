"""app.anthropic_cost（集計・分類・予測の純粋ロジック）の単体テスト。"""

from datetime import date

from app import anthropic_cost as ac


def test_classify_kind_real_examples():
    # 2026/06 の実領収書の明細をそのまま判定できること
    assert ac.classify_kind("Auto-recharge credits") == "api_credit"
    assert ac.classify_kind("Prepaid extra usage, Individual plan") == "extra_usage"
    assert ac.classify_kind("Auto recharge extra usage, Individual plan") == "extra_usage"
    assert ac.classify_kind("Claude Max plan subscription") == "subscription"
    assert ac.classify_kind("Something else") == "other"
    assert ac.classify_kind("") == "other"
    assert ac.classify_kind(None) == "other"


def test_month_key_and_days():
    assert ac.month_key(date(2026, 6, 11)) == "2026-06"
    assert ac.days_in_month(date(2026, 6, 11)) == 30
    assert ac.days_in_month(date(2026, 2, 1)) == 28  # 2026年は平年


def test_project_month_end():
    # 11日間で $3,745.60 → 30日換算
    assert ac.project_month_end(3745.60, date(2026, 6, 11)) == 10215.27
    # 1日目でもゼロ除算しない
    assert ac.project_month_end(100.0, date(2026, 6, 1)) == 3000.0
    assert ac.project_month_end(0.0, date(2026, 6, 15)) == 0.0


def test_to_jpy():
    assert ac.to_jpy(995.35, 160.47) == 159724
    assert ac.to_jpy(100.0, None) is None
    assert ac.to_jpy(100.0, 0) is None
    assert ac.to_jpy(0.0, 160.0) == 0


def test_humanize_jpy():
    assert ac.humanize_jpy(0) == "0円"
    assert ac.humanize_jpy(6300) == "6,300円"
    assert ac.humanize_jpy(601060) == "60.1万円"
    assert ac.humanize_jpy(None) == "0円"


def test_recent_month_keys():
    keys = ac.recent_month_keys(date(2026, 6, 11), 3)
    assert keys == ["2026-04", "2026-05", "2026-06"]
    # 年またぎ
    keys = ac.recent_month_keys(date(2026, 1, 5), 3)
    assert keys == ["2025-11", "2025-12", "2026-01"]


def test_assemble_monthly_zero_fill_and_kinds():
    months = ["2026-05", "2026-06"]
    rows = [
        {"month": "2026-06", "kind": "extra_usage", "total_usd": 2530.15, "total_jpy": 406178, "count": 3},
        {"month": "2026-06", "kind": "api_credit", "total_usd": 220.10, "total_jpy": 35320, "count": 1},
        {"month": "2026-04", "kind": "api_credit", "total_usd": 999.0, "total_jpy": 1, "count": 1},  # 範囲外→無視
        {"month": "2026-06", "kind": "mystery", "total_usd": 1.0, "total_jpy": 160, "count": 1},  # 未知kind→other
    ]
    out = ac.assemble_monthly(rows, months)
    assert out[0] == {
        "month": "2026-05",
        "total_usd": 0.0,
        "total_jpy": 0,
        "receipts": 0,
        "by_kind": {"api_credit": 0.0, "extra_usage": 0.0, "subscription": 0.0, "other": 0.0},
    }
    cur = out[1]
    assert cur["total_usd"] == 2751.25
    assert cur["total_jpy"] == 441658
    assert cur["receipts"] == 5
    assert cur["by_kind"]["extra_usage"] == 2530.15
    assert cur["by_kind"]["api_credit"] == 220.10
    assert cur["by_kind"]["other"] == 1.0
