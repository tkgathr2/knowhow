"""こえキングのチャンク化・純粋ロジック（DB非依存）。

発話（kb_utterances 相当の dict）を「話題ウィンドウ」へまとめ、検索資産（kb_chunks）に
入れるテキストとメタを組み立てる。LLM呼び出し・DB・embedding はここには持ち込まない。

Phase 0 の分割方針:
  連続する発話を最大文字数（既定 ~1500）で詰めて区切る単純窓。話題境界の自動検出は
  LLM が要るため後続フェーズに送る（YAGNI）。これでも「あの会話どこ？」検索には十分効く。
"""

from __future__ import annotations

import json

DEFAULT_MAX_CHARS = 1500


def _finalize(group: list[dict]) -> dict:
    speakers: list[str] = []
    for u in group:
        if u["speaker"] not in speakers:
            speakers.append(u["speaker"])
    return {
        "seq_start": group[0]["seq"],
        "seq_end": group[-1]["seq"],
        "speakers": speakers,
        "start_ms": group[0]["start_ms"],
        "end_ms": group[-1]["end_ms"],
        "lines": [{"speaker": u["speaker"], "content": u["content"]} for u in group],
    }


def chunk_utterances(utterances: list[dict], max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """発話列（seq昇順）を話題ウィンドウに分割する。

    1発話が単独で max_chars を超える場合もその発話だけで1チャンクにする（取りこぼさない）。
    """
    chunks: list[dict] = []
    cur: list[dict] = []
    cur_len = 0
    for u in utterances:
        line_len = len(u.get("content") or "") + len(u.get("speaker") or "") + 2
        if cur and cur_len + line_len > max_chars:
            chunks.append(_finalize(cur))
            cur = []
            cur_len = 0
        cur.append(u)
        cur_len += line_len
    if cur:
        chunks.append(_finalize(cur))
    return chunks


def build_chunk_content(chunk: dict, title: str | None = None, recorded_at=None) -> str:
    """チャンクを検索用テキストへ。先頭に「日時 / タイトル / 参加者」ヘッダを付ける。"""
    header_parts: list[str] = []
    if recorded_at is not None:
        header_parts.append(str(recorded_at))
    if title:
        header_parts.append(title)
    if chunk.get("speakers"):
        header_parts.append("／".join(chunk["speakers"]))
    header = "【" + " / ".join(header_parts) + "】" if header_parts else ""
    body = "\n".join(f"{ln['speaker']}: {ln['content']}" for ln in chunk["lines"])
    return f"{header}\n{body}" if header else body


def parse_tags(raw: str | None, max_tags: int = 6) -> list[str]:
    """LLM出力から話題タグの配列を頑健に取り出す。失敗時は []。

    受理する形: JSON配列 ["a","b"] / ```json で囲まれた配列 / 読点・改行区切りの素テキスト。
    """
    if not raw:
        return []
    s = raw.strip()
    # コードフェンス除去
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # JSON配列をまず試す
    start = s.find("[")
    end = s.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(s[start : end + 1])
            if isinstance(arr, list):
                return _clean_tags(arr, max_tags)
        except (json.JSONDecodeError, ValueError):
            pass
    # 区切り文字でのフォールバック
    for sep in ("、", ",", "\n", "・"):
        if sep in s:
            return _clean_tags(s.split(sep), max_tags)
    return _clean_tags([s], max_tags)


def _clean_tags(items: list, max_tags: int) -> list[str]:
    out: list[str] = []
    for it in items:
        t = str(it).strip().strip("\"'").strip("# ").strip()
        if t and t not in out:
            out.append(t)
        if len(out) >= max_tags:
            break
    return out
