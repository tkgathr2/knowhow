"""Anthropic費用ダッシュボードの純粋ロジック（集計・分類・予測）。

ルーター（routers/anthropic_cost.py）から呼ばれる。DBアクセスは持たない。
"""

from __future__ import annotations

import calendar
from datetime import date

# 種別の表示名（ダッシュボードと共有する正規キー）
KINDS = ("api_credit", "extra_usage", "subscription", "other")


def classify_kind(description: str) -> str:
    """領収書の明細文字列から種別を判定する。

    実例（2026/06 実測）:
      - "Auto-recharge credits"                        → api_credit（Console APIクレジット）
      - "Prepaid extra usage, Individual plan"         → extra_usage（Claude 追加利用・前払い）
      - "Auto recharge extra usage, Individual plan"   → extra_usage（Claude 追加利用・自動）
      - "Claude Max plan" / "subscription"             → subscription（プラン月額）
    """
    d = (description or "").lower()
    if "extra usage" in d:
        return "extra_usage"
    if "credit" in d:
        return "api_credit"
    if "subscription" in d or "plan" in d:
        # "Individual plan" 単体は上の2分岐で先に拾われるため、ここに来る "plan" は月額系
        return "subscription"
    return "other"


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def project_month_end(mtd_total: float, today: date) -> float:
    """月初からの実績(mtd_total)を経過日数で日割りし、月末着地を単純予測する。"""
    elapsed = max(1, today.day)
    return round(mtd_total / elapsed * days_in_month(today), 2)


def to_jpy(total_usd: float, usdjpy: float | None) -> int | None:
    if usdjpy is None or usdjpy <= 0:
        return None
    return int(round((total_usd or 0.0) * usdjpy))


def humanize_jpy(jpy: int | float | None) -> str:
    n = int(jpy or 0)
    if abs(n) >= 10_000:
        return f"{n / 10_000:.1f}万円"
    return f"{n:,}円"


def assemble_monthly(rows: list[dict], months: list[str]) -> list[dict]:
    """月キー昇順のリスト(months)に沿って月次集計を0埋めで並べる。

    rows: [{month, kind, total_usd, total_jpy, count}] の生集計。
    """
    by_month: dict[str, dict] = {
        m: {
            "month": m,
            "total_usd": 0.0,
            "total_jpy": 0,
            "receipts": 0,
            "by_kind": {k: 0.0 for k in KINDS},
        }
        for m in months
    }
    for r in rows:
        m = by_month.get(r["month"])
        if m is None:
            continue
        m["total_usd"] = round(m["total_usd"] + float(r["total_usd"] or 0), 2)
        m["total_jpy"] += int(r["total_jpy"] or 0)
        m["receipts"] += int(r["count"] or 0)
        kind = r["kind"] if r["kind"] in m["by_kind"] else "other"
        m["by_kind"][kind] = round(m["by_kind"][kind] + float(r["total_usd"] or 0), 2)
    return [by_month[m] for m in months]


def recent_month_keys(today: date, n: int) -> list[str]:
    """当月を末尾に、過去nヶ月分の "YYYY-MM" を昇順で返す。"""
    keys: list[str] = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))
