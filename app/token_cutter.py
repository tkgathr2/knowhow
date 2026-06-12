"""トークンカッターくん実績の純粋集計ロジック（DB非依存・単体テスト可能）。

入力は期間別の件数・推定トークンの dict。日次の時系列に組み立てる。
正直な指標: 「重い手を検知して助言した回数」と「その時に避けられた推定トークン」。
助言に実際に従ったかは判定できないため "推定削減" として扱う（確定額とは言わない）。
"""

from __future__ import annotations

import os

# 金額換算の前提（上限見積り）。避けられるのは「大型Read/広域Grep が食う入力トークン」
# なので、入力トークン単価で換算する。Claude Code は Opus 実行なので Opus 4.x の
# 入力 list 価格 $15 / 100万トークンを既定にする。為替は環境変数で上書き可。
DEFAULT_USD_PER_MTOK = float(os.getenv("TOKEN_CUTTER_USD_PER_MTOK", "15.0"))
DEFAULT_USDJPY = float(os.getenv("TOKEN_CUTTER_USDJPY", "150.0"))


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


def estimate_money(
    tokens: int,
    usd_per_mtok: float = DEFAULT_USD_PER_MTOK,
    usdjpy: float = DEFAULT_USDJPY,
) -> dict:
    """推定削減トークンを金額（USD / JPY）に換算する（上限見積り）。

    避けられるのは入力トークンなので入力単価で換算。確定額ではなく
    「もし全部の助言に従っていたら避けられた上限額」である点は UI 側で明記する。
    """
    tokens = max(0, int(tokens or 0))
    usd = round(tokens / 1_000_000 * usd_per_mtok, 2)
    jpy = int(round(usd * usdjpy))
    return {
        "usd": usd,
        "jpy": jpy,
        "usd_per_mtok": usd_per_mtok,
        "usdjpy": usdjpy,
    }


def humanize_jpy(jpy: int) -> str:
    """円を読みやすく（>=1万→万、それ未満はカンマ区切り相当の素の値）。"""
    jpy = int(jpy or 0)
    if jpy >= 10_000:
        return f"{round(jpy / 10_000, 1)}万円"
    return f"{jpy:,}円"


def share_pct(part: int, total: int) -> float:
    """全体に占める割合（％・小数1桁）。total<=0 は 0.0。"""
    if total <= 0:
        return 0.0
    return round(int(part or 0) / total * 100, 1)


def with_shares(rows: list[dict], total_tokens: int) -> list[dict]:
    """name/count/est_tokens の各行へ、削減トークンの占有率(token_pct)を付ける。"""
    out: list[dict] = []
    for r in rows:
        out.append({**r, "token_pct": share_pct(r.get("est_tokens", 0), total_tokens)})
    return out
