"""部長別ナレッジ集計の純粋ロジック（DB非依存・単体テスト可能）。

5部長制（社長指示 2026/06/07）に沿って、ナレッジの1件1件を
「どの部長の領域の知識か」に分類する。判定は
①project_key の明示マップ → ②タグ/本文のキーワード → ③既定値 の順。
"""

from __future__ import annotations

# 部長の定義（表示順もこの順）
BUCHO_DEFS: list[dict] = [
    {"key": "sanada", "name": "真田 啓", "title": "開発部長", "emoji": "🛠️",
     "domain": "システム開発・AI・インフラ", "color": "#4f46e5"},
    {"key": "muroi", "name": "室井 剛", "title": "オペレーション部長（COO）", "emoji": "🔄",
     "domain": "現場オペ・総務・業務標準化", "color": "#0d9488"},
    {"key": "kujo", "name": "九条 玲", "title": "財務部長（CFO）", "emoji": "💰",
     "domain": "お金・経理・数字", "color": "#d97706"},
    {"key": "kirishima", "name": "霧島 章吾", "title": "法務部長（CLO）", "emoji": "⚖️",
     "domain": "契約・法律・コンプライアンス", "color": "#be123c"},
    {"key": "todo", "name": "藤堂 一馬", "title": "経営管理部長", "emoji": "🧭",
     "domain": "戦略・人事・組織・リスク", "color": "#7c3aed"},
    {"key": "common", "name": "全社共通", "title": "どの部にも効く知恵", "emoji": "🏢",
     "domain": "仕事の進め方・共通ノウハウ", "color": "#6b7280"},
]

BUCHO_KEYS = [b["key"] for b in BUCHO_DEFS]

# ① project_key の明示マップ（開発システムは原則 真田）
PROJECT_MAP: dict[str, str] = {
    # 真田（開発システム群）
    "knowhow": "sanada", "security-report-system": "sanada", "cast-meibo": "sanada",
    "recruit": "sanada", "takagi_iride": "sanada", "slack-mioshi": "sanada",
    "procast-sync": "sanada", "daily-report-automation-mvp": "sanada", "prorepo": "sanada",
    "seiko": "sanada", "k-timecard": "sanada", "kaizen-mado": "sanada", "houko": "sanada",
    "zenmie": "sanada", "junkai-kun": "sanada", "stepup_contract_maker": "sanada",
    "slack_channel_Downloader": "sanada", "sns-ban-checker": "sanada",
    "auto-backlog": "sanada", "google_auth": "sanada", "factory": "sanada",
    "nky-db": "sanada", "eisenhower": "sanada", "bulk": "sanada",
    "DEVIN_CONSTITUTION": "sanada", "devin-constitution": "sanada",
    "crewai-agents": "sanada", "demo": "sanada", "demo2": "sanada",
    "test-bulk": "sanada", "TEST_BUG_CHECK": "sanada",
    # 九条（お金・数字）
    "monthly-cf": "kujo",
    # 藤堂（人事・組織）
    "kotsuyudo-hr-automation": "todo",
    # 室井（現業オペ・見守り）
    "ohayo-kazuko": "muroi", "kazuko_departure_watch": "muroi",
}

# project_key で決まらないときの既定値（cto-lab は全セッション混在 → キーワードで振り分け）
DEFAULT_MAP: dict[str, str] = {"cto-lab": "sanada", "general": "common"}

# ② キーワード（タグ＋本文先頭）。先に並ぶ部長ほど優先。
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("kujo", ("資金繰り", "銀行", "融資", "返済", "経理", "会計", "試算表", "請求書", "見積",
              "単価", "キャッシュ", "月次決算", "マネーフォワード", "MFクラウド", "財務",
              "仕訳", "助成金", "補助金", "支払", "入金", "売掛", "買掛")),
    ("kirishima", ("契約", "法務", "許認可", "警備業法", "派遣法", "反社", "訴訟", "コンプラ",
                   "規約", "著作権", "商標", "法的", "暴排", "個人情報保護")),
    ("todo", ("人事", "採用", "評価制度", "等級", "組織", "戦略", "リスク管理", "統制",
              "離職", "定着", "教育", "在留資格", "外国人材", "面接")),
    ("muroi", ("総務", "オペレーション", "業務標準", "属人化", "現場", "勤怠", "シフト",
               "発注", "備品", "物品", "庶務", "体裁ルール", "納品物")),
]


def classify(project_key: str, tags: list[str] | None, content_head: str = "") -> str:
    """1件のナレッジを部長キーへ分類する。"""
    pk = (project_key or "").strip()
    if pk in PROJECT_MAP:
        return PROJECT_MAP[pk]
    text = " ".join(tags or []) + " " + (content_head or "")
    for key, words in KEYWORDS:
        if any(w in text for w in words):
            return key
    return DEFAULT_MAP.get(pk, "common")


def aggregate(rows: list[dict], since_iso: str, prev_since_iso: str) -> list[dict]:
    """分類済みの行（classify 適用前の生データ）から部長別カードを作る。

    rows: {project_key, tags, content_head, created_at(ISO str), recall_count}
    since_iso / prev_since_iso: 今期間・前期間の開始日時（ISO・文字列比較で足りる形式）。
    """
    stats = {k: {"total": 0, "added": 0, "added_prev": 0, "recalls": 0} for k in BUCHO_KEYS}
    for r in rows:
        key = classify(r.get("project_key", ""), r.get("tags"), r.get("content_head", ""))
        s = stats[key]
        s["total"] += 1
        s["recalls"] += int(r.get("recall_count") or 0)
        created = str(r.get("created_at") or "")
        if created >= since_iso:
            s["added"] += 1
        elif created >= prev_since_iso:
            s["added_prev"] += 1

    out: list[dict] = []
    for d in BUCHO_DEFS:
        s = stats[d["key"]]
        prev = s["added_prev"]
        growth_pct = round((s["added"] - prev) / prev * 100, 1) if prev > 0 else None
        out.append({**d, **s, "growth_pct": growth_pct})
    return out
