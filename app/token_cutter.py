"""コストカッターくん実績の純粋集計ロジック（DB非依存・単体テスト可能）。

入力は期間別の件数・推定トークンの dict。日次の時系列に組み立てる。
正直な指標: 「重い手を検知して助言した回数」と「その時に避けられた推定トークン」。
助言に実際に従ったかは判定できないため "推定削減" として扱う（確定額とは言わない）。
"""

from __future__ import annotations


def daily_keys_desc(*by_day: dict[str, object]) -> list[str]:
    keys: set[str] = set()
    for d in by_day:
        keys.update(d.keys())
    return sorted(keys, reverse=True)


def assemble_daily(
    days_desc: list[str],
    events_by_day: dict[str, int],
    tokens_by_day: dict[str, int],
) -> list[dict]:
    """日別の {date, events, est_tokens} を組み立てる（新しい日が先頭）。"""
    return [
        {
            "date": day,
            "events": events_by_day.get(day, 0),
            "est_tokens": tokens_by_day.get(day, 0),
        }
        for day in days_desc
    ]


def humanize_tokens(n: int) -> str:
    """推定トークンを読みやすい単位に（>=1M→M、>=1k→k）。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{round(n / 1_000_000, 1)}M"
    if n >= 1_000:
        return f"{round(n / 1_000, 1)}k"
    return str(n)
