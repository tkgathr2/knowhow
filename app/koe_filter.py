"""ロア（録音資産）の会話判定フィルタ（純粋ロジック・DB/LLM非依存）。

PLAUD は終日つけっぱなしで、機内アナウンス・PA放送・移動中の雑音なども拾う。
それらを取り込むとダイジェスト・検索がノイズだらけになるため、
「持ち主（社長）が実際に会話している録音」だけを ingested とし、それ以外は noise として
チャンク化・ダイジェストの対象から外す。

判定は軽量ヒューリスティック（LLM不要・即時・無料）。閾値は実データで調整可能。
"""

from __future__ import annotations

# 録音の持ち主（社長）の話者ラベル表記ゆれ。build_utterances の正規化前(raw)・後どちらも拾う。
OWNER_LABELS = {"Atsuhiro Takagi", "髙木豊大", "高木豊大", "Takagi", "takagi"}

# 定型アナウンス／放送に頻出する語。これらが支配的な録音は会話ではない。
ANNOUNCE_MARKERS = (
    "シートベルト", "客室乗務員", "キャビンアテンダント", "非常口", "救命",
    "safety", "airplane mode", "emergency", "passengers", "seat belt",
    "ただいま電話が大変混み合", "お電話ありがとうございました", "おかけ直し",
    "本日はご利用", "まもなく到着", "次は,", "次は、", "ドアが閉まり", "発車いたします",
    "ご乗車ありがとう", "黄色い線", "白線の内側", "扉が閉まります",
)


def _is_owner(u: dict) -> bool:
    return (u.get("speaker") in OWNER_LABELS) or (u.get("speaker_raw") in OWNER_LABELS)


def conversation_score(utterances: list[dict]) -> dict:
    """会話らしさの指標を返す（判定の根拠を可視化する）。"""
    n = len(utterances)
    if n == 0:
        return {"n": 0, "owner": 0, "owner_ratio": 0.0, "speakers": 0, "announce_ratio": 0.0}
    owner = sum(1 for u in utterances if _is_owner(u))
    speakers = len({u.get("speaker") for u in utterances if u.get("speaker")})
    announce = 0
    for u in utterances:
        c = u.get("content") or ""
        if any(m in c for m in ANNOUNCE_MARKERS):
            announce += 1
    return {
        "n": n,
        "owner": owner,
        "owner_ratio": round(owner / n, 3),
        "speakers": speakers,
        "announce_ratio": round(announce / n, 3),
    }


def is_conversation(utterances: list[dict]) -> tuple[bool, str, dict]:
    """会話（取り込む価値あり）か判定。戻り値 = (会話か, 理由, スコア)。

    判定方針:
      - アナウンス語が支配的（4割以上）→ noise（機内/PA放送）
      - 持ち主が2回以上発話し、発話比率が一定以上 → 会話
      - 2人以上の話者がいて持ち主も登場し、アナウンスが少ない → 会話（対話）
      - それ以外（持ち主ほぼ不在・単一話者の環境音など）→ noise
    """
    s = conversation_score(utterances)
    if s["n"] == 0:
        return (False, "no_utterances", s)
    if s["announce_ratio"] >= 0.4:
        return (False, "announcement_dominant", s)
    if s["owner"] >= 2 and s["owner_ratio"] >= 0.15:
        return (True, "owner_active", s)
    # 多人数の対話：持ち主が1回でも登場すること必須（owner不在の英語PA放送等の偽陽性を防ぐ）。
    # 社長の録音デバイス前提なので「社長が一度も出ない会話」は取り込まない（落としても noise 台帳に残り救済可）。
    if s["speakers"] >= 2 and s["announce_ratio"] < 0.2 and s["owner"] >= 1:
        return (True, "dialog", s)
    return (False, "low_signal_or_ambient", s)
