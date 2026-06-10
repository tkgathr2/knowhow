"""成長ダッシュボード用の純粋ロジック（DB非依存・単体テスト可能）。

ノウハウキングの「どれだけ・どう・何％成長したか」を、DBから取った
期間別の集計（dict）だけを入力に組み立てる。SQL/DBアクセスは router 側、
ここは計算だけに専念させることで、now を注入してテストできるようにする。
"""

from __future__ import annotations

import calendar
from datetime import datetime


def period_label(dt: datetime, bucket: str) -> str:
    """日時を期間ラベルへ。month='YYYY-MM' / week='IYYY-Www'（ISO週）。

    Postgres 側の to_char(date_trunc(...), 'YYYY-MM' / 'IYYY-"W"IW') と
    必ず同じ文字列になるよう揃える（突き合わせの要）。
    """
    if bucket == "week":
        iso = dt.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    return f"{dt.year:04d}-{dt.month:02d}"


def days_in_period(period: str, bucket: str) -> int:
    """その期間の総日数。週は7、月はその月の実日数。"""
    if bucket == "week":
        return 7
    year, month = (int(x) for x in period.split("-"))
    return calendar.monthrange(year, month)[1]


def elapsed_in_current(now: datetime, bucket: str) -> int:
    """現在の期間で既に経過した日数（最低1）。月=now.day / 週=ISO曜日(1..7)。"""
    if bucket == "week":
        return now.isoweekday()
    return now.day


def build_points(
    added_by_period: dict[str, int],
    deprecated_by_period: dict[str, int],
) -> list[dict]:
    """期間別の追加件数から、累計と前期比成長率を載せた時系列を作る。

    growth_pct = 当期追加 / 前期末累計 * 100（前期末累計=0 のときは None＝「新規」）。
    """
    points: list[dict] = []
    cumulative = 0
    for period in sorted(added_by_period):
        added = added_by_period[period]
        prev_cumulative = cumulative
        cumulative += added
        growth_pct = (
            round(added / prev_cumulative * 100, 1) if prev_cumulative > 0 else None
        )
        points.append(
            {
                "period": period,
                "added": added,
                "cumulative": cumulative,
                "growth_pct": growth_pct,
                "deprecated_added": deprecated_by_period.get(period, 0),
            }
        )
    return points


def project_current(points: list[dict], now: datetime, bucket: str) -> dict | None:
    """最新期間が「今まさに進行中の期間」なら、日割りで着地を予測する。

    最新データが現在期間でなければ None（途中経過の予測を出さない）。
    """
    if not points:
        return None
    last = points[-1]
    if last["period"] != period_label(now, bucket):
        return None
    elapsed = max(1, elapsed_in_current(now, bucket))
    total_days = days_in_period(last["period"], bucket)
    prev_cumulative = last["cumulative"] - last["added"]
    projected_added = round(last["added"] / elapsed * total_days)
    projected_growth_pct = (
        round(projected_added / prev_cumulative * 100, 1)
        if prev_cumulative > 0
        else None
    )
    return {
        "period": last["period"],
        "added_so_far": last["added"],
        "days_elapsed": elapsed,
        "days_in_period": total_days,
        "projected_added": projected_added,
        "projected_growth_pct": projected_growth_pct,
    }


_SERIES_NOUN = {"asset": "正味の学び", "log": "取込ログ", "all": "ナレッジ"}


def make_narrative(points: list[dict], current: dict | None, series: str = "asset") -> str:
    """画面トップの一言サマリ（自動生成）。数字の羅列を解釈に変える。"""
    if not points:
        return "まだ成長データがありません。"
    noun = _SERIES_NOUN.get(series, "ナレッジ")
    last = points[-1]
    text = f"{last['period']} は{noun} +{last['added']}件"
    if last["growth_pct"] is not None:
        text += f"（前期比 +{last['growth_pct']}%）"
    if current is not None and len(points) >= 2:
        prev_added = points[-2]["added"]
        proj = current["projected_added"]
        if prev_added > 0:
            if proj > prev_added:
                text += f"。今期は着地 約{proj}件 見込みで加速中"
            elif proj < prev_added:
                text += f"。今期は着地 約{proj}件 見込みでペース鈍化"
            else:
                text += f"。今期は着地 約{proj}件 見込みで横ばい"
    elif len(points) >= 2:
        prev_added = points[-2]["added"]
        if last["added"] > prev_added:
            text += "。前期より加速"
        elif last["added"] < prev_added:
            text += "。前期より鈍化"
    return text + "。"


def helpful_rate(helpful: int, unhelpful: int) -> float | None:
    """有用度の割合。評価が1件も無ければ None（未評価）。"""
    denom = helpful + unhelpful
    if denom <= 0:
        return None
    return round(helpful / denom * 100, 1)


def vectorized_pct(embedded: int, total: int) -> float:
    """ベクトル化率（健全性指標）。"""
    if total <= 0:
        return 0.0
    return round(embedded / total * 100, 1)
