"""Phase S1 採点配管の純粋関数単体テスト。DB非依存。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.routers.nightly import attribute_outcome, clamp_delta

# ---------------------------------------------------------------------------
# attribute_outcome
# ---------------------------------------------------------------------------

def _dt(offset_hours: float = 0.0) -> datetime:
    """基準時刻から offset_hours ずらした UTC datetime を返すヘルパ。"""
    base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
    return base + timedelta(hours=offset_hours)


def test_attribute_outcome_success_in_window():
    recall = _dt(0)
    sessions = [(_dt(2), "success")]
    assert attribute_outcome(recall, sessions, window_hours=6) == "success"


def test_attribute_outcome_fail_in_window():
    recall = _dt(0)
    sessions = [(_dt(3), "fail")]
    assert attribute_outcome(recall, sessions, window_hours=6) == "fail"


def test_attribute_outcome_session_outside_window_returns_none():
    recall = _dt(0)
    # window=6h → 7h後は対象外
    sessions = [(_dt(7), "success")]
    assert attribute_outcome(recall, sessions, window_hours=6) is None


def test_attribute_outcome_no_sessions_returns_none():
    recall = _dt(0)
    assert attribute_outcome(recall, [], window_hours=6) is None


def test_attribute_outcome_partial_returns_none():
    recall = _dt(0)
    sessions = [(_dt(1), "partial")]
    assert attribute_outcome(recall, sessions, window_hours=6) is None


def test_attribute_outcome_multiple_sessions_first_wins():
    """複数セッションがある場合は窓内で最初の success/fail を返す。"""
    recall = _dt(0)
    sessions = [
        (_dt(1), "success"),
        (_dt(2), "fail"),
    ]
    # 最初に来るのは success → success を返す
    assert attribute_outcome(recall, sessions, window_hours=6) == "success"


def test_attribute_outcome_fail_then_success_first_is_fail():
    recall = _dt(0)
    sessions = [
        (_dt(1), "fail"),
        (_dt(3), "success"),
    ]
    assert attribute_outcome(recall, sessions, window_hours=6) == "fail"


def test_attribute_outcome_session_before_recall_ignored():
    """recall より前のセッションは無視する。"""
    recall = _dt(5)
    sessions = [
        (_dt(2), "success"),   # recall より前 → 無視
        (_dt(7), "fail"),      # recall より後、窓内 → 採用
    ]
    assert attribute_outcome(recall, sessions, window_hours=6) == "fail"


def test_attribute_outcome_exactly_at_window_end_excluded():
    """created_at == window_end は対象外（half-open [start, end)）。"""
    recall = _dt(0)
    sessions = [(_dt(6), "success")]   # ちょうど6h後 = window_end → 含まない
    assert attribute_outcome(recall, sessions, window_hours=6) is None


def test_attribute_outcome_just_before_window_end_included():
    recall = _dt(0)
    sessions = [(_dt(5.999), "success")]
    assert attribute_outcome(recall, sessions, window_hours=6) == "success"


# ---------------------------------------------------------------------------
# clamp_delta
# ---------------------------------------------------------------------------

def test_clamp_delta_normal():
    """累計 0 から step 0.2 → そのまま 0.2 加算。"""
    assert clamp_delta(0.0, 0.2, 1.0) == pytest.approx(0.2)


def test_clamp_delta_partial_cap():
    """累計 0.9 + step 0.2 → cap=1.0 なので実際の加算は 0.1。"""
    assert clamp_delta(0.9, 0.2, 1.0) == pytest.approx(0.1)


def test_clamp_delta_already_at_cap():
    """累計が既に cap に達している場合は 0 を返す。"""
    assert clamp_delta(1.0, 0.2, 1.0) == pytest.approx(0.0)


def test_clamp_delta_over_cap_returns_zero():
    """累計が cap を超えた場合も 0（負にならない）。"""
    assert clamp_delta(1.5, 0.2, 1.0) == pytest.approx(0.0)


def test_clamp_delta_step_larger_than_cap():
    """step > cap のとき残量分だけ加算。"""
    assert clamp_delta(0.0, 5.0, 1.0) == pytest.approx(1.0)


def test_clamp_delta_zero_current():
    """初期状態でちょうど cap と同じ step → cap 丸ごと加算。"""
    assert clamp_delta(0.0, 1.0, 1.0) == pytest.approx(1.0)
