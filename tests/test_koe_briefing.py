"""koe_briefing（経営ブリーフィング整形・純粋ロジック）の単体テスト。"""

from app import koe_briefing


def _sig(t, title, imp, who=None, detail=None):
    return {"signal_type": t, "title": title, "importance": imp, "who": who, "detail": detail}


def test_empty_briefing():
    md = koe_briefing.build_briefing_markdown("2026-06-15", [])
    assert "2026-06-15" in md
    assert "ありません" in md


def test_counts_all_types_present():
    c = koe_briefing.summarize_counts([_sig("risk", "x", 5), _sig("risk", "y", 4), _sig("decision", "z", 9)])
    assert c["risk"] == 2 and c["decision"] == 1 and c["opportunity"] == 0


def test_briefing_groups_and_sorts_by_importance():
    sigs = [
        _sig("risk", "低リスク", 3),
        _sig("decision", "重い判断", 9, who="社長"),
        _sig("risk", "高リスク", 8, detail="火種"),
    ]
    md = koe_briefing.build_briefing_markdown("2026-06-15", sigs)
    # 種別の並びは decision が risk より先（_TYPE_ORDER）
    assert md.index("要判断") < md.index("リスク")
    # リスク内は重要度降順（高リスク8 が 低リスク3 より上）
    assert md.index("高リスク") < md.index("低リスク")
    # who / detail / 重要度が出る
    assert "〔社長〕" in md
    assert "火種" in md
    assert "[重要度9]" in md


def test_headline_lists_nonzero_types_only():
    md = koe_briefing.build_briefing_markdown("2026-06-15", [_sig("opportunity", "好機", 7)])
    head = md.splitlines()[2]  # 見出しの集計行
    assert "好機 1件" in head
    assert "リスク" not in head  # 0件の種別は集計行に出さない
