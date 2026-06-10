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


def pct_change(curr: int, prev: int) -> float | None:
    """前期比（%）。前期0は None（割れない＝新規）。"""
    if prev <= 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def make_weekly_narrative(d: dict) -> str:
    """週次成長サマリの一言（自動生成）。今週 vs 先週を解釈に変える。"""
    parts: list[str] = []
    ch = pct_change(d.get("asset_now", 0), d.get("asset_prev", 0))
    chs = f"（前週比 {'+' if (ch or 0) >= 0 else ''}{ch}%）" if ch is not None else ""
    parts.append(f"今週の学び +{d.get('asset_now', 0)}件{chs}")
    if d.get("recalls_now"):
        parts.append(f"／想起 {d['recalls_now']}回")
    if d.get("helpful_rate") is not None:
        parts.append(f"／有用度 {d['helpful_rate']}%")
    if d.get("deprecated_now"):
        parts.append(f"／新陳代謝 {d['deprecated_now']}件沈下")
    tail = ""
    if d.get("util_pct") is not None:
        tail = f" 資産の活用率 {d['util_pct']}%・未活用 {d.get('never_recalled', 0)}件。"
    return "".join(parts) + "。" + tail


def attach_daily_growth(entries_desc: list[dict], base_before: int) -> list[dict]:
    """日次エントリ（新しい順）へ、累計資産と前日比成長率を付ける。

    base_before = 集計期間より前に既にあった「正味ナレッジ資産」の累計。
    各日 growth_pct = その日の asset_added / 前日終了時点の累計 × 100
    （前日終了時点が 0 のときは None＝新規で割れない）。資産(asset_added)だけを
    成長とみなす（取込ログ log_added は分母・分子に入れない）。
    """
    cumulative = max(0, int(base_before or 0))
    annotated_asc: list[dict] = []
    for e in reversed(entries_desc):  # 古い日から累積する
        added = int(e.get("asset_added", 0))
        prev = cumulative
        cumulative += added
        growth_pct = round(added / prev * 100, 1) if prev > 0 else None
        annotated_asc.append({**e, "asset_cumulative": cumulative, "growth_pct": growth_pct})
    annotated_asc.reverse()  # 新しい順へ戻す
    return annotated_asc


def latest_daily_growth(entries_desc: list[dict]) -> dict | None:
    """最新日の前日比サマリ（画面の見出し用）。entries は attach 済みを渡す。"""
    if not entries_desc:
        return None
    top = entries_desc[0]
    return {
        "date": top.get("date"),
        "asset_added": int(top.get("asset_added", 0)),
        "asset_cumulative": int(top.get("asset_cumulative", 0)),
        "growth_pct": top.get("growth_pct"),
    }


def daily_keys(*by_day_dicts: dict[str, object]) -> list[str]:
    """各 dict の日付キーの和集合を、新しい日付が先頭になるよう降順で返す。"""
    keys: set[str] = set()
    for d in by_day_dicts:
        keys.update(d.keys())
    return sorted(keys, reverse=True)


def assemble_daily(
    days_desc: list[str],
    asset_by_day: dict[str, int],
    log_by_day: dict[str, int],
    dep_by_day: dict[str, int],
    recalled_by_day: dict[str, int],
    items_by_day: dict[str, list],
    per_day_item_cap: int = 20,
) -> list[dict]:
    """日付ごとに「その日に何が増えた/使われた/沈んだか」を1エントリへまとめる。

    items は «その日に追加された正味のナレッジ資産» の抜粋。多すぎる日は cap で
    切り、切った件数を items_truncated に残す（黙って捨てない）。
    """
    out: list[dict] = []
    for day in days_desc:
        items = items_by_day.get(day, [])
        out.append(
            {
                "date": day,
                "asset_added": asset_by_day.get(day, 0),
                "log_added": log_by_day.get(day, 0),
                "deprecated": dep_by_day.get(day, 0),
                "recalled": recalled_by_day.get(day, 0),
                "items": items[:per_day_item_cap],
                "items_truncated": max(0, len(items) - per_day_item_cap),
            }
        )
    return out
