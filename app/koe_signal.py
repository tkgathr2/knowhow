"""ロア（録音資産）からの「経営判断シグナル」抽出の純粋ロジック（DB・LLM非依存）。

秋好モデル③：日次ダイジェストと同じ録音テキストから、LLM が
「社長が知る/判断すべきこと」だけを構造化 JSON で返す。ここはその LLM 出力を
頑健にパース・正規化し、保存可能な行（dict）へ落とす部分だけを担う（単体テスト可能）。
LLM 呼び出し本体は koe_tag.extract_signals（外部I/O）に置く。
"""

from __future__ import annotations

import hashlib
import json
import re

# 種別（経営判断に効くものだけ）。未知種別は "other" に倒す。
SIGNAL_TYPES: tuple[str, ...] = (
    "decision",     # 社長が決めるべき/判断待ち
    "risk",         # リスク・火種・トラブルの芽
    "opportunity",  # 好機・商談・改善余地
    "promise",      # 誰かが約束した宿題・TODO・コミット
    "complaint",    # 人の不満・離職の芽・現場の声
    "number",       # 重要な数字・KPI の変化
    "other",        # その他（拾うが優先度は下げる）
)

# 日本語ラベル → 内部種別の寄せ（LLM が日本語で返した場合の保険）
_TYPE_ALIASES: dict[str, str] = {
    "判断": "decision", "意思決定": "decision", "要判断": "decision", "決裁": "decision",
    "リスク": "risk", "懸念": "risk", "問題": "risk", "火種": "risk",
    "好機": "opportunity", "機会": "opportunity", "商談": "opportunity", "チャンス": "opportunity",
    "約束": "promise", "宿題": "promise", "todo": "promise", "タスク": "promise", "コミット": "promise",
    "不満": "complaint", "苦情": "complaint", "離職": "complaint",
    "数字": "number", "kpi": "number", "数値": "number",
}

_MAX_TITLE = 200
_MAX_DETAIL = 1000
_MAX_WHO = 120


def normalize_type(raw: str | None) -> str:
    """LLM の種別文字列を内部種別へ正規化（未知は other）。"""
    if not raw:
        return "other"
    s = str(raw).strip().lower()
    if s in SIGNAL_TYPES:
        return s
    return _TYPE_ALIASES.get(s, "other")


def clamp_importance(raw: object, default: int = 5) -> int:
    """重要度を 1〜10 に丸める。数値化できなければ default。"""
    try:
        n = int(round(float(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(10, n))


def _trim(s: object, limit: int) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    return t[:limit]


def dedup_hash(signal_type: str, title: str) -> str:
    """同一日内の重複判定キー。種別＋タイトルの正規化（記号/空白除去・小文字）でハッシュ。"""
    norm = re.sub(r"\s+", "", f"{signal_type}|{title}").lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _extract_json_array(raw: str) -> list | None:
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        arr = json.loads(s[start : end + 1])
        return arr if isinstance(arr, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


def parse_signals(raw: str | None, max_signals: int = 30) -> list[dict]:
    """LLM 出力（JSON配列）から正規化済みシグナル行を取り出す。失敗時は []。

    受理する各要素: {"type","title","detail","who","importance"}（欠けは許容）。
    title が空の要素は捨てる。dedup_hash を付与する。種別/重要度は正規化。
    同一バッチ内の重複（同 dedup_hash）も 1 件に畳む。
    """
    if not raw:
        return []
    arr = _extract_json_array(raw)
    if arr is None:
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for item in arr:
        if not isinstance(item, dict):
            continue
        title = _trim(item.get("title") or item.get("見出し") or item.get("内容"), _MAX_TITLE)
        if not title:
            continue
        stype = normalize_type(item.get("type") or item.get("種別") or item.get("category"))
        h = dedup_hash(stype, title)
        if h in seen:
            continue
        seen.add(h)
        rows.append(
            {
                "signal_type": stype,
                "title": title,
                "detail": _trim(item.get("detail") or item.get("詳細") or item.get("背景"), _MAX_DETAIL),
                "who": _trim(item.get("who") or item.get("誰") or item.get("対象"), _MAX_WHO),
                "importance": clamp_importance(item.get("importance") or item.get("重要度")),
                "dedup_hash": h,
            }
        )
        if len(rows) >= max_signals:
            break
    return rows
