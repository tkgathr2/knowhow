"""nightly の重複統合（純粋ロジック部分）の単体テスト。DB非依存。"""

import time

import pytest

from app.routers.nightly import merge_stats, pick_keeper, select_merge_pairs


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


# ---------------------------------------------------------------------------
# select_merge_pairs: 近傍(KNN)検索の生ペア整形（旧 O(n²) 自己結合の置換に伴う新ロジック）
# 生ペア形式: (id_a, conf_a, id_b, conf_b, similarity)
# ---------------------------------------------------------------------------

def test_select_merge_pairs_canonicalizes_id_order():
    # 近傍検索は候補→近傍の向きで (id_a > id_b) もあり得る。小さいid側を id_a に正準化し、
    # 信頼度も一緒に入れ替えてペアリングを保つ。
    out = select_merge_pairs([(20, 0.9, 10, 0.3, 0.97)], max_pairs=10)
    assert out == [(10, 0.3, 20, 0.9, 0.97)]


def test_select_merge_pairs_dedupes_reciprocal_keeps_max_similarity():
    # a が b を、b が a を近傍に引くと同一ペアが2回出る。canonical 化して1件に畳み、
    # 類似度は最大値を採る。
    out = select_merge_pairs(
        [
            (10, 0.3, 20, 0.9, 0.95),  # a→b
            (20, 0.9, 10, 0.3, 0.98),  # b→a（より高い類似度）
        ],
        max_pairs=10,
    )
    assert out == [(10, 0.3, 20, 0.9, 0.98)]


def test_select_merge_pairs_drops_self_pairs():
    # id_a == id_b（自己一致）は捨てる
    out = select_merge_pairs([(5, 0.5, 5, 0.5, 1.0)], max_pairs=10)
    assert out == []


def test_select_merge_pairs_sorted_by_similarity_desc_and_capped():
    raw = [
        (1, 0.5, 2, 0.5, 0.90),
        (3, 0.5, 4, 0.5, 0.99),
        (5, 0.5, 6, 0.5, 0.95),
    ]
    out = select_merge_pairs(raw, max_pairs=2)
    # 類似度降順で上位2件のみ
    assert [p[4] for p in out] == [0.99, 0.95]


def test_select_merge_pairs_stable_tiebreak_by_ids():
    # 同類似度は (id_a, id_b) 昇順で決定的に並ぶ
    raw = [
        (7, 0.5, 8, 0.5, 0.96),
        (1, 0.5, 2, 0.5, 0.96),
        (3, 0.5, 4, 0.5, 0.96),
    ]
    out = select_merge_pairs(raw, max_pairs=10)
    assert [(p[0], p[2]) for p in out] == [(1, 2), (3, 4), (7, 8)]


def test_select_merge_pairs_empty_and_zero_cap():
    assert select_merge_pairs([], max_pairs=10) == []
    assert select_merge_pairs([(1, 0.5, 2, 0.5, 0.99)], max_pairs=0) == []


def test_select_merge_pairs_count_regression_respects_cap_after_dedupe():
    # 件数回帰: 重複・往復ペアを大量に混ぜても、出力は「ユニークなcanonicalペア数」かつ
    # max_pairs を超えない。旧 O(n²) では検出件数が爆発したが、本関数は入力に対し線形で
    # max_pairs に必ず丸まることを担保する。
    raw = []
    for i in range(0, 200, 2):
        a, b = i + 1, i + 2
        sim = 0.90 + (i % 10) / 1000.0
        raw.append((a, 0.5, b, 0.5, sim))   # a→b
        raw.append((b, 0.5, a, 0.5, sim))   # b→a（往復＝重複）
    out = select_merge_pairs(raw, max_pairs=50)
    assert len(out) == 50  # 100ユニークペア中、上限50に丸まる
    # 全て canonical（id_a < id_b）かつ往復重複が消えている
    assert all(p[0] < p[2] for p in out)
    assert len({(p[0], p[2]) for p in out}) == len(out)


def test_select_merge_pairs_performance_is_linear_bounded():
    # 性能回帰: 大量(5万)生ペアでも線形時間で素早く処理し切る（O(n²)退行の番人）。
    n = 50_000
    raw = [(i * 2 + 1, 0.5, i * 2 + 2, 0.5, 0.90 + (i % 100) / 10000.0) for i in range(n)]
    start = time.perf_counter()
    out = select_merge_pairs(raw, max_pairs=50)
    elapsed = time.perf_counter() - start
    assert len(out) == 50
    assert elapsed < 2.0  # 5万件を2秒未満（CIの余裕を見た緩い上限）
