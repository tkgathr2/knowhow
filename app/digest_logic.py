"""1日のダイジェスト生成の純粋ロジック（DB非依存・単体テスト可能）。

「1日346件は読めない」（社長 2026/06/13）への答え。
その日の学び一覧から、素人が読める日本語3〜5行のまとめを作る。
LLM（gpt-4o-mini）が使えれば自然文、使えなければルールベースで必ず文章を返す。
"""

from __future__ import annotations

from collections import Counter

# LLMへ渡す1日分の素材の上限（コストと入力長の安全弁）
MAX_ITEMS_FOR_LLM = 60
MAX_ITEM_CHARS = 160

SYSTEM_PROMPT = (
    "あなたは会社の知識係です。AIが1日に学んだことのリストを読み、"
    "ITを知らない経営者が「今日、会社が何を賢くなったのか」を実感できる、"
    "読み応えのある日本語の振り返りにまとめます。\n"
    "\n"
    "【絶対ルール】\n"
    "- 専門用語（チャンク・ベクトル・想起・デプロイ・API・エンドポイント等）は禁止。"
    "使うときは『AIの記憶』『思い出して使う』のように日常語へ言い換える\n"
    "- 事実だけ。リストに無いことは書かない。誇張しない\n"
    "\n"
    "【見出し（headline）】\n"
    "- その日を一言で表す、25字以内のタイトル。何が前進したかが伝わるもの\n"
    "\n"
    "【本文（body）】6〜9文。次の流れで物語のように書く：\n"
    "1. 今日いちばんの収穫を1〜2文で。『何ができなかったのが、何でできるようになったか』"
    "『どんな失敗を、これからは防げるようになったか』という"
    "“ビフォー→アフター”の形で具体的に書く\n"
    "2. それを支える具体的な学びを2〜3件、かみ砕いて紹介する（どの仕事・どのシステムの話かも添える）\n"
    "3. その学びが次にどう役立つか（再発防止・時短・コスト減・判断材料 など）を1文で\n"
    "4. 最後に数字で締める：今日増えた学びの件数、前の日と比べた伸び、"
    "過去の知恵が実際に使われた回数など。手応えが伝わるように\n"
    "\n"
    "硬い箇条書きにせず、社長に語りかけるような自然な文章で。"
    "出力は必ずJSON: {\"headline\": \"...\", \"body\": \"...\"}"
)


def build_llm_input(date: str, stats: dict, items: list[dict]) -> str:
    """LLMに渡すその日の素材テキストを組み立てる。"""
    lines = [
        f"日付: {date}",
        f"増えた学び: {stats.get('asset_added', 0)}件 / "
        f"自動記録ログ: {stats.get('log_added', 0)}件 / "
        f"使われた知識: {stats.get('recalled', 0)}件 / "
        f"整理(引退)した知識: {stats.get('deprecated', 0)}件",
    ]
    if stats.get("growth_pct") is not None:
        lines.append(f"前の日と比べた伸び: {stats['growth_pct']:+}%")
    if stats.get("asset_cumulative"):
        lines.append(f"この日までにたまった知恵の累計: {stats['asset_cumulative']}件")
    lines.append("--- その日に増えた学び（抜粋） ---")
    for it in items[:MAX_ITEMS_FOR_LLM]:
        tags = ",".join((it.get("tags") or [])[:4])
        content = (it.get("content") or "")[:MAX_ITEM_CHARS].replace("\n", " ")
        lines.append(f"[{it.get('project_key', '')}] ({tags}) {content}")
    if len(items) > MAX_ITEMS_FOR_LLM:
        lines.append(f"…ほか {len(items) - MAX_ITEMS_FOR_LLM} 件")
    return "\n".join(lines)


def top_topics(items: list[dict], n: int = 3) -> list[str]:
    """タグの頻度から、その日の主なトピックを取り出す（フォールバック文用）。"""
    skip = {"auto-ingest", "curated", "学び", "session_log", "session-archive-202606"}
    counter: Counter[str] = Counter()
    for it in items:
        for t in it.get("tags") or []:
            t = str(t).strip()
            if t and t not in skip and not t.startswith("session"):
                counter[t] += 1
    return [t for t, _ in counter.most_common(n)]


def fallback_digest(date: str, stats: dict, items: list[dict]) -> dict:
    """LLMが使えないときのルールベース・ダイジェスト。必ず読める文章を返す。"""
    added = int(stats.get("asset_added", 0))
    recalled = int(stats.get("recalled", 0))
    deprecated = int(stats.get("deprecated", 0))
    projects = Counter(it.get("project_key", "") for it in items if it.get("project_key"))

    if added <= 0 and recalled <= 0:
        return {
            "headline": "静かな1日",
            "body": f"{date}は新しい学びの追加はありませんでした。",
        }

    parts: list[str] = []
    parts.append(f"この日は新しい知恵が{added}件たまりました。")
    topics = top_topics(items)
    if topics:
        parts.append(f"主なテーマは「{'」「'.join(topics)}」。")
    if projects:
        top_pj, top_cnt = projects.most_common(1)[0]
        if len(projects) > 1:
            parts.append(
                f"いちばん学びが多かった仕事は「{top_pj}」（{top_cnt}件）で、"
                f"ほか{len(projects) - 1}分野でも知恵が増えています。"
            )
        else:
            parts.append(f"学びはすべて「{top_pj}」の仕事からです。")
    if recalled > 0:
        parts.append(f"過去にためた知識が{recalled}件、実際の仕事で役立ちました。")
    if deprecated > 0:
        parts.append(f"古くなった知識{deprecated}件は整理しました。")

    headline = f"知恵が{added}件増えた日" if added > 0 else "知識が活躍した日"
    return {"headline": headline, "body": "".join(parts)}


def normalize_llm_digest(raw: dict | None, date: str, stats: dict, items: list[dict]) -> dict:
    """LLM出力を検証し、欠け・崩れがあればフォールバックで埋める。"""
    if not isinstance(raw, dict):
        return fallback_digest(date, stats, items)
    headline = str(raw.get("headline") or "").strip()[:40]
    body = str(raw.get("body") or "").strip()[:2000]
    if not headline or not body:
        return fallback_digest(date, stats, items)
    return {"headline": headline, "body": body}
