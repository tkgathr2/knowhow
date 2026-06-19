"""経営ブリーフィング（秋好モデル④「朝起きたら把握」）の純粋ロジック（DB・LLM非依存）。

kb_signals から取り出したシグナル群を、社長が朝ひと目で把握できる Markdown へ整形する。
LLM は使わず決定論で組み立てる（速い・安い・毎回同じ・テスト可能）。
"""

from __future__ import annotations

# 表示順・絵文字・日本語ラベル（重要度の高い経営インパクト順）
_TYPE_ORDER: list[tuple[str, str, str]] = [
    ("decision", "🟥", "要判断"),
    ("risk", "⚠️", "リスク"),
    ("opportunity", "💡", "好機"),
    ("promise", "✅", "宿題（約束）"),
    ("complaint", "🗣️", "現場の声・不満"),
    ("number", "📊", "重要な数字"),
    ("other", "📌", "その他"),
]


def summarize_counts(signals: list[dict]) -> dict[str, int]:
    """種別ごとの件数（0 を含む全種別）。"""
    counts = {key: 0 for key, _, _ in _TYPE_ORDER}
    for s in signals:
        t = s.get("signal_type", "other")
        counts[t] = counts.get(t, 0) + 1
    return counts


def _headline(date_label: str, counts: dict[str, int], total: int) -> str:
    if total == 0:
        return f"## ☀️ {date_label} の経営ブリーフィング\n\nこの日に拾うべきシグナルはありませんでした。"
    bits = [f"{label} {counts[key]}件" for key, _, label in _TYPE_ORDER if counts.get(key)]
    return f"## ☀️ {date_label} の経営ブリーフィング\n\n**{' / '.join(bits)}**"


def build_briefing_markdown(date_label: str, signals: list[dict]) -> str:
    """シグナル群を朝ブリーフィング Markdown へ。重要度の高い順に種別ごとへまとめる。

    signals: [{signal_type,title,detail,who,importance}] を想定（status は呼び出し側で絞る）。
    """
    total = len(signals)
    parts: list[str] = [_headline(date_label, summarize_counts(signals), total)]
    if total == 0:
        return parts[0]

    by_type: dict[str, list[dict]] = {}
    for s in signals:
        by_type.setdefault(s.get("signal_type", "other"), []).append(s)

    for key, emoji, label in _TYPE_ORDER:
        items = by_type.get(key)
        if not items:
            continue
        items = sorted(items, key=lambda x: x.get("importance", 0), reverse=True)
        parts.append(f"\n### {emoji} {label}（{len(items)}）")
        for s in items:
            who = f" 〔{s['who']}〕" if s.get("who") else ""
            imp = s.get("importance", 0)
            parts.append(f"- **[重要度{imp}]** {s.get('title', '')}{who}")
            if s.get("detail"):
                parts.append(f"    - {s['detail']}")
    return "\n".join(parts)
