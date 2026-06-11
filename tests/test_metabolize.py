"""metabolize（学びの新陳代謝）の純粋ロジック部分の単体テスト。DB非依存。"""

from app.routers.metabolize import (
    DEFAULT_REASON,
    dedupe_ids,
    meta_with_deprecation,
    normalize_reason,
    partition_apply_results,
)


def test_meta_with_deprecation_adds_audit_fields():
    meta = {"notion_learning_id": "abc"}
    out = meta_with_deprecation(meta, "metabolized", "2026-06-11T00:00:00+00:00")
    assert out["deprecated_reason"] == "metabolized"
    assert out["deprecated_at"] == "2026-06-11T00:00:00+00:00"
    # 既存キーは保持（非破壊の追記）
    assert out["notion_learning_id"] == "abc"


def test_meta_with_deprecation_does_not_mutate_input():
    meta = {"k": "v"}
    out = meta_with_deprecation(meta, "r", "t")
    # 元 dict は変更しない（JSONB の変更検知のため新 dict を返す設計）
    assert meta == {"k": "v"}
    assert out is not meta


def test_meta_with_deprecation_handles_none():
    out = meta_with_deprecation(None, "metabolized", "t")
    assert out == {"deprecated_reason": "metabolized", "deprecated_at": "t"}


def test_normalize_reason_defaults_when_blank():
    assert normalize_reason(None) == DEFAULT_REASON
    assert normalize_reason("") == DEFAULT_REASON
    assert normalize_reason("   ") == DEFAULT_REASON
    assert normalize_reason(" stale ") == "stale"


def test_dedupe_ids_keeps_order():
    assert dedupe_ids([3, 1, 3, 2, 1]) == [3, 1, 2]
    assert dedupe_ids([]) == []


def test_partition_apply_results():
    requested = [1, 2, 3, 4]
    # 1=未deprecated, 2=既にdeprecated, 3=未deprecated, 4=DBに無い
    found = {1: False, 2: True, 3: False}
    to_dep, already, not_found = partition_apply_results(requested, found)
    assert to_dep == [1, 3]
    assert already == [2]
    assert not_found == [4]


def test_partition_apply_results_all_missing():
    to_dep, already, not_found = partition_apply_results([10, 20], {})
    assert to_dep == []
    assert already == []
    assert not_found == [10, 20]
