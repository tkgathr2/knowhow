"""koe_signal（経営判断シグナル抽出の純粋ロジック）の単体テスト。"""

from app import koe_signal


def test_normalize_type_known_and_alias_and_unknown():
    assert koe_signal.normalize_type("decision") == "decision"
    assert koe_signal.normalize_type("RISK") == "risk"
    assert koe_signal.normalize_type("リスク") == "risk"
    assert koe_signal.normalize_type("約束") == "promise"
    assert koe_signal.normalize_type("謎の種別") == "other"
    assert koe_signal.normalize_type(None) == "other"


def test_clamp_importance():
    assert koe_signal.clamp_importance(5) == 5
    assert koe_signal.clamp_importance(99) == 10
    assert koe_signal.clamp_importance(0) == 1
    assert koe_signal.clamp_importance("8") == 8
    assert koe_signal.clamp_importance("なし") == 5  # default
    assert koe_signal.clamp_importance(None) == 5


def test_dedup_hash_is_stable_and_normalized():
    a = koe_signal.dedup_hash("risk", "資金繰りが厳しい")
    b = koe_signal.dedup_hash("risk", "資金繰りが厳しい")
    c = koe_signal.dedup_hash("risk", "資金繰りが  厳しい")  # 空白差は同一視
    assert a == b == c
    assert a != koe_signal.dedup_hash("decision", "資金繰りが厳しい")


def test_parse_signals_basic_array():
    raw = (
        '[{"type":"decision","title":"A社との契約を継続するか",'
        '"detail":"値上げ要求あり","who":"社長","importance":9}]'
    )
    rows = koe_signal.parse_signals(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["signal_type"] == "decision"
    assert r["title"] == "A社との契約を継続するか"
    assert r["who"] == "社長"
    assert r["importance"] == 9
    assert len(r["dedup_hash"]) == 32


def test_parse_signals_handles_code_fence_and_japanese_keys():
    raw = '```json\n[{"種別":"リスク","見出し":"離職の兆し","重要度":7}]\n```'
    rows = koe_signal.parse_signals(raw)
    assert len(rows) == 1
    assert rows[0]["signal_type"] == "risk"
    assert rows[0]["title"] == "離職の兆し"
    assert rows[0]["importance"] == 7


def test_parse_signals_skips_titleless_and_dedups_in_batch():
    raw = (
        '[{"type":"risk","title":"X"},'
        '{"type":"risk","title":"X"},'           # 同一 → 1件に畳む
        '{"type":"risk","detail":"no title"}]'    # title 無し → 捨てる
    )
    rows = koe_signal.parse_signals(raw)
    assert len(rows) == 1


def test_parse_signals_empty_and_garbage():
    assert koe_signal.parse_signals(None) == []
    assert koe_signal.parse_signals("") == []
    assert koe_signal.parse_signals("これはJSONではない") == []
    assert koe_signal.parse_signals("[]") == []


def test_parse_signals_respects_max():
    items = ",".join(f'{{"type":"other","title":"t{i}"}}' for i in range(50))
    rows = koe_signal.parse_signals(f"[{items}]", max_signals=10)
    assert len(rows) == 10
