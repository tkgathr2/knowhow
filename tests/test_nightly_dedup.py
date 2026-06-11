"""nightly の重複統合（純粋ロジック部分）の単体テスト。DB非依存。"""

import pytest

from app.routers.nightly import merge_stats, pick_keeper


def test_pick_keeper_higher_confidence_wins():
    # b の方が信頼度が高い → b を残す
    assert pick_keeper(1, 0.7, 2, 0.9) == (2, 1)
    # a の方が高い → a を残す
    assert pick_keeper(1, 0.9, 2, 0.7) == (1, 2)


def test_pick_keeper_tie_keeps_older():
    # 同点なら古い方（id小=先に作られた方）を残す
    assert pick_keeper(10, 0.8, 20, 0.8) == (10, 20)


def test_merge_stats_combines_alpha_beta():
    # 事前分布(1,1)の二重計上を除いて合算（既存 merge-duplicates と同一数式）
    alpha, beta, conf = merge_stats(9.0, 1.0, 5.0, 2.0)
    assert alpha == 13.0  # 9 + 5 - 1
    assert beta == 2.0    # 1 + 2 - 1
    assert conf == pytest.approx(13.0 / 15.0)


def test_merge_stats_fresh_pair_stays_neutral():
    # 初期値同士(1,1)+(1,1) → (1,1) のまま＝信頼度0.5を維持
    alpha, beta, conf = merge_stats(1.0, 1.0, 1.0, 1.0)
    assert (alpha, beta) == (1.0, 1.0)
    assert conf == pytest.approx(0.5)


def test_merge_stats_confidence_in_unit_interval():
    alpha, beta, conf = merge_stats(6.0, 2.0, 4.0, 2.0)
    assert 0.0 < conf < 1.0
