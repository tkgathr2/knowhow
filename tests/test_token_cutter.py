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
