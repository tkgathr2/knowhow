"""コストカッターα: 削減率の純粋ロジック（DB非依存・単体テスト可能）。

2つの実データから「いくら下げられたか」を出す:
  - anthropic-cost（実費用の月次 total_jpy）: ピーク月 → 当月着地予測 の削減額/削減率。
  - token-cutter（推定節約）: ゲートが避けた推定トークンの金額（別モジュールで換算）。

DBアクセスは持たない。ルーター（routers/cost_cutter.py）から呼ばれる。
"""

from __future__ import annotations


def pick_baseline(monthly: list[dict], current_month: str) -> dict | None:
    """基準（ピーク）月を選ぶ。

    当月（未完了で過小評価される）を除いた完了済み月のうち、実費用 total_jpy が
    最大の月を基準にする。完了済みで費用>0 の月が無ければ None。
    monthly: [{"month": "YYYY-MM", "total_jpy": int}] の昇順想定（順不同でも可）。
    """
    complete = [
        m
        for m in monthly
        if m.get("month") != current_month and int(m.get("total_jpy") or 0) > 0
    ]
    if not complete:
        return None
    return max(complete, key=lambda m: int(m.get("total_jpy") or 0))


def reduction(baseline_jpy: int, projection_jpy: int) -> dict:
    """基準額 → 当月着地予測 の削減額と削減率。

    増えている（projection >= baseline）場合は削減0・率0.0（マイナス表示はしない）。
    baseline<=0 のときも率0.0（比較対象が無い）。
    """
    baseline_jpy = int(baseline_jpy or 0)
    projection_jpy = int(projection_jpy or 0)
    cut = max(0, baseline_jpy - projection_jpy)
    pct = round(cut / baseline_jpy * 100, 1) if baseline_jpy > 0 else 0.0
    return {"reduction_jpy": cut, "reduction_pct": pct}


def annualized_saving(baseline_jpy: int, projection_jpy: int) -> int:
    """月次の削減額（基準-着地予測）を12倍した年間削減見込み。増加時は0。"""
    cut = max(0, int(baseline_jpy or 0) - int(projection_jpy or 0))
    return cut * 12


# 種別の日本語ラベル（コストカッターαの「ムダ可視化」用）。
# subscription = Maxプラン月額（必要経費）。それ以外は overage＝割引ゼロの買い足し＝削れる対象。
KIND_LABELS = {
    "subscription": "Maxプラン月額",
    "extra_usage": "追加利用（割引ゼロ）",
    "api_credit": "APIクレジット",
    "other": "その他",
}
# 「ムダ（overage）」＝サブスク月額以外。本来は使用量を枠内に収めれば消える買い足し。
OVERAGE_KINDS = ("extra_usage", "api_credit", "other")


def assemble_breakdown(by_kind_jpy: dict[str, int]) -> list[dict]:
    """当月の種別内訳を {kind, label, jpy} の固定順リストにする（0埋め）。"""
    order = ("subscription", "extra_usage", "api_credit", "other")
    return [
        {"kind": k, "label": KIND_LABELS[k], "jpy": int(by_kind_jpy.get(k) or 0)}
        for k in order
    ]


def overage_jpy(by_kind_jpy: dict[str, int]) -> int:
    """ムダ＝サブスク月額以外（追加利用・クレジット・その他）の合計。"""
    return sum(int(by_kind_jpy.get(k) or 0) for k in OVERAGE_KINDS)
