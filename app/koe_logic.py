"""こえキング（録音資産化）の純粋ロジック。

DB に依存しない部分（話者正規化・発話整形・登場人物集計・取込状態判定）を
切り出して単体テスト可能にする。ルーター（app/routers/koe.py）はここを呼ぶ。
"""

from __future__ import annotations

from app.textutil import sanitize_utf8


def normalize_speaker(raw: str | None, aliases: dict[str, str]) -> str:
    """Plaud の話者ラベルをエイリアス表で正規化する。

    未知ラベルは原文のまま通す（運用で digest 報告→人がエイリアス登録する）。
    None/空は "unknown" に倒す（NOT NULL 制約を満たすため）。
    """
    s = sanitize_utf8(raw).strip() if raw else ""
    if not s:
        return "unknown"
    return aliases.get(s, s)


def build_utterances(segments: list[dict], aliases: dict[str, str]) -> list[dict]:
    """Plaud raw transcript の segment 配列を kb_utterances 行へ整形する。

    - 空 content（無音区切り等）はスキップ
    - seq は 0 始まりの連番（保存順の安定キー）
    - speaker は正規化、speaker_raw に原ラベルを保持
    入力 segment 例: {start_time, end_time, content, speaker, original_speaker}
    """
    rows: list[dict] = []
    seq = 0
    for seg in segments:
        content = sanitize_utf8(seg.get("content")).strip() if seg.get("content") else ""
        if not content:
            continue
        raw = seg.get("speaker") or seg.get("original_speaker")
        rows.append(
            {
                "seq": seq,
                "speaker": normalize_speaker(raw, aliases),
                "speaker_raw": (raw or None),
                "start_ms": int(seg.get("start_time") or 0),
                "end_ms": int(seg.get("end_time") or 0),
                "content": content,
            }
        )
        seq += 1
    return rows


def speaker_set(utterances: list[dict]) -> list[str]:
    """発話行から登場人物（正規化後）の一覧を、登場順を保ったまま重複排除して返す。"""
    seen: list[str] = []
    for u in utterances:
        sp = u.get("speaker")
        if sp and sp not in seen:
            seen.append(sp)
    return seen


def decide_status(has_transcript: bool, utterance_count: int) -> str:
    """取込時の transcript_status を決める。

    - 文字起こし未生成 → pending（生成は plaud-generate-all 任せ・翌日 watermark が回収）
    - 生成済みだが有効発話 0 → empty（無音・雑音のみ）
    - それ以外 → ingested
    """
    if not has_transcript:
        return "pending"
    if utterance_count == 0:
        return "empty"
    return "ingested"


def unknown_speakers(utterances: list[dict], aliases: dict[str, str]) -> list[str]:
    """エイリアス表に載っていない話者ラベル（=要人手登録）を抽出する。digest 報告用。"""
    out: list[str] = []
    for u in utterances:
        raw = u.get("speaker_raw")
        if raw and raw not in aliases and raw not in out:
            out.append(raw)
    return out
