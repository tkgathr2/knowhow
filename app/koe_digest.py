"""ロア（Lore）日次ダイジェストの純粋ロジック（DB・LLM非依存）。

その日の録音群（録音メタ＋発話）から LLM へ渡す入力テキストを組み立て、
LLM が無い/失敗した場合の機械フォールバック要約も用意する。
LLM 呼び出し本体は koe_tag.generate_daily_digest（外部I/O）に置く。
"""

from __future__ import annotations

# LLM に渡す発話本文の総量上限（トークン暴発・コスト/レイテンシ対策）
MAX_SOURCE_CHARS = 24000


def build_digest_source(date_label: str, recordings: list[dict], max_chars: int = MAX_SOURCE_CHARS) -> str:
    """その日の録音群を 1 本の LLM 入力テキストへ。

    recordings: [{title, recorded_at, speakers, lines:[{speaker,content}]}]（時刻昇順を想定）
    上限を超えたら古い順に詰め、超過分は「…(以下省略)」で打ち切る（先頭=その日の早い時間を優先）。
    """
    parts: list[str] = [f"# {date_label} の録音記録"]
    used = len(parts[0])
    truncated = False
    for rec in recordings:
        header_bits = []
        if rec.get("recorded_at"):
            header_bits.append(str(rec["recorded_at"]))
        if rec.get("title"):
            header_bits.append(rec["title"])
        if rec.get("speakers"):
            header_bits.append("参加者: " + "／".join(rec["speakers"]))
        block_head = "\n## " + " / ".join(header_bits) if header_bits else "\n## 録音"
        lines = [f"{ln['speaker']}: {ln['content']}" for ln in rec.get("lines", [])]
        block = block_head + "\n" + "\n".join(lines)
        if used + len(block) > max_chars:
            remain = max_chars - used
            if remain > 0:
                parts.append(block[:remain])
            truncated = True
            break
        parts.append(block)
        used += len(block)
    if truncated:
        parts.append("\n…(以下省略)")
    return "\n".join(parts)


def fallback_digest(date_label: str, recordings: list[dict]) -> str:
    """LLM が使えない時の機械フォールバック（録音件数・参加者・タイトルの素朴な一覧）。"""
    if not recordings:
        return f"【{date_label}】録音はありませんでした。"
    speakers: list[str] = []
    for rec in recordings:
        for sp in rec.get("speakers", []):
            if sp and sp not in speakers:
                speakers.append(sp)
    lines = [f"【{date_label} のダイジェスト（簡易版）】", f"- 録音 {len(recordings)} 件"]
    if speakers:
        lines.append(f"- 登場した人: {'、'.join(speakers)}")
    lines.append("- 主な録音:")
    for rec in recordings[:10]:
        title = rec.get("title") or "（無題）"
        t = rec.get("recorded_at") or ""
        lines.append(f"  - {t} {title}")
    lines.append("\n※AI要約は利用できなかったため簡易版です（OpenAIキー未設定または一時障害）。")
    return "\n".join(lines)


DIGEST_SYSTEM_PROMPT = (
    "あなたは社長専属の有能な秘書です。以下はある1日の録音（会話の文字起こし）です。"
    "社長が後で振り返れるよう、日本語のMarkdownで経営ダイジェストにまとめてください。"
    "その日に複数の異なる議題・案件があれば、必ず議題ごとに分けて1件ずつまとめること（全部を1つに溶かさない）。\n"
    "次の構成で出力すること:\n"
    "## 今日のサマリ（3〜5行）\n"
    "\n"
    "## トピック別ポイント\n"
    "（その日の主要な議題・案件ごとに見出しを立て、1件ずつまとめる。議題が1つならそれだけでよい。"
    "各トピックは次の小項目で:）\n"
    "### ① <議題名（短く）>\n"
    "- 要点: …\n"
    "- 決めたこと / 次アクション: …（あれば。無ければ省略）\n"
    "### ② <議題名>\n"
    "…（議題の数だけ続ける）\n"
    "\n"
    "## 約束・宿題（誰が・何を・いつまでに が分かれば添える。全トピック横断で一覧）\n"
    "## 人物別トピック（主要な相手ごとに何を話したか）\n"
    "## 気になる発言・要注意（リスク・チャンスの芽）\n"
    "雑談や無関係な部分は省く。事実に無いことは書かない。簡潔に、箇条書き中心で。"
)
