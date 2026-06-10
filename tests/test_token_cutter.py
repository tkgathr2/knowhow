"""app.token_cutter（実績集計の純粋ロジック）の単体テスト。"""

from app import token_cutter as tc


def test_daily_keys_desc_union():
    keys = tc.daily_keys_desc(
        {"2026-06-08": 1, "2026-06-10": 2},
        {"2026-06-09": 5},
    )
    assert keys == ["2026-06-10", "2026-06-09", "2026-06-08"]


def test_assemble_daily():
    rows = tc.assemble_daily(
        ["2026-06-10", "2026-06-09"],
        events_by_day={"2026-06-10": 3},
        tokens_by_day={"2026-06-10": 41000, "2026-06-09": 0},
    )
    assert rows[0] == {"date": "2026-06-10", "events": 3, "est_tokens": 41000}
    # 件数の無い日は 0 埋め
    assert rows[1] == {"date": "2026-06-09", "events": 0, "est_tokens": 0}


def test_assemble_daily_empty():
    assert tc.assemble_daily([], {}, {}) == []


def test_humanize_tokens():
    assert tc.humanize_tokens(0) == "0"
    assert tc.humanize_tokens(950) == "950"
    assert tc.humanize_tokens(20500) == "20.5k"
    assert tc.humanize_tokens(2_500_000) == "2.5M"
    assert tc.humanize_tokens(None) == "0"


def test_estimate_money():
    # 280万トークン @ $15/Mtok・150円/$ = $42 → 6,300円
    m = tc.estimate_money(2_800_000, usd_per_mtok=15.0, usdjpy=150.0)
    assert m["usd"] == 42.0
    assert m["jpy"] == 6300
    assert m["usd_per_mtok"] == 15.0
    assert m["usdjpy"] == 150.0
    # 0・負・None は 0 円
    assert tc.estimate_money(0)["jpy"] == 0
    assert tc.estimate_money(None)["usd"] == 0.0
    assert tc.estimate_money(-100)["jpy"] == 0


def test_humanize_jpy():
    assert tc.humanize_jpy(0) == "0円"
    assert tc.humanize_jpy(6300) == "6,300円"
    assert tc.humanize_jpy(63000) == "6.3万円"
    assert tc.humanize_jpy(None) == "0円"


def test_share_pct():
    assert tc.share_pct(25, 100) == 25.0
    assert tc.share_pct(1, 3) == 33.3
    assert tc.share_pct(5, 0) == 0.0  # 分母0は0%
    assert tc.share_pct(0, 100) == 0.0


def test_with_shares():
    rows = [
        {"name": "large_read", "count": 5, "est_tokens": 750},
        {"name": "broad_grep", "count": 2, "est_tokens": 250},
    ]
    out = tc.with_shares(rows, 1000)
    assert out[0]["token_pct"] == 75.0
    assert out[1]["token_pct"] == 25.0
    # 元の行は壊さない（新キー追加のみ）
    assert out[0]["count"] == 5
