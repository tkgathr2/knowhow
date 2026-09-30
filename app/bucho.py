"""部長別ナレッジ集計の純粋ロジック（DB非依存・単体テスト可能）。

専務＋9部長＋社長室室長＋全社共通の体制（2026-06-14時点）に沿って、ナレッジの1件1件を
「どの役の領域の知識か」に分類する。判定は
①project_key の明示マップ → ②タグ/本文のキーワード → ③既定値 の順。
専務 鷹司（senmu）は領域を持たないが、横断案件の裁定/完遂ログを senmu キーで集計し可視化する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 日次集計は社長がJSTで見る前提のため、日付境界をJSTで切る
# （app/routers/koe.py・anthropic_cost.py・cost_cutter.py と同じ既存パターン踏襲）。
_JST = timezone(timedelta(hours=9))


def _to_jst_date(iso_str: str) -> str:
    """ISO日時文字列(created_at等)をJSTの日付(YYYY-MM-DD)へ変換する。

    パース失敗時は元の先頭10文字（旧来のUTC文字列切り出し）にフォールバックする。
    """
    s = (iso_str or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d")
    except ValueError:
        return s[:10]

# 部長の定義（表示順もこの順。ただしフロント(/bucho)は rank=svp を先頭固定で並べ替える）
BUCHO_DEFS: list[dict] = [
    {"key": "kagura", "name": "神楽 迅", "title": "AI・DX担当 副社長（CAIO/CDXO）", "emoji": "🤖",
     "domain": "生成AI活用・業務自動化・DX・データ基盤・社長対話窓口", "color": "#16a34a", "rank": "svp"},
    {"key": "senmu", "name": "鷹司 統", "title": "専務取締役（執行No.2）", "emoji": "🎩",
     "domain": "全社統括・部長間の裁定・優先順位・完遂", "color": "#334155", "rank": "bucho"},
    {"key": "sanada", "name": "真田 啓", "title": "開発部長", "emoji": "🛠️",
     "domain": "システム開発・AI・インフラ", "color": "#4f46e5", "rank": "bucho"},
    {"key": "muroi", "name": "室井 剛", "title": "オペレーション部長（COO）", "emoji": "🔄",
     "domain": "現場オペ・総務・業務標準化", "color": "#0d9488", "rank": "bucho"},
    {"key": "kujo", "name": "九条 玲", "title": "財務部長（CFO）", "emoji": "💰",
     "domain": "お金・経理・数字", "color": "#d97706", "rank": "bucho"},
    {"key": "kirishima", "name": "霧島 章吾", "title": "法務部長（CLO）", "emoji": "⚖️",
     "domain": "契約・法律・コンプライアンス", "color": "#be123c", "rank": "bucho"},
    {"key": "todo", "name": "藤堂 一馬", "title": "経営管理部長", "emoji": "🧭",
     "domain": "戦略・人事・組織・リスク", "color": "#7c3aed", "rank": "bucho"},
    {"key": "kaburagi", "name": "鏑木 蓮", "title": "マーケティング部長（CMO）", "emoji": "📣",
     "domain": "集客・広告・リード・CRM/LTV", "color": "#ea580c", "rank": "bucho"},
    {"key": "kuze", "name": "久世 澪", "title": "デザイン部長（CDO）", "emoji": "🎨",
     "domain": "UI/UX・画面/LP設計・デザインシステム", "color": "#db2777", "rank": "bucho"},
    {"key": "kuon", "name": "久遠 颯", "title": "ブランディング部長（CBO）", "emoji": "✨",
     "domain": "ブランド戦略・パーパス・ネーミング・トンマナ", "color": "#0891b2", "rank": "bucho"},
    {"key": "saotome", "name": "早乙女 静", "title": "社長室室長", "emoji": "🏛️",
     "domain": "振り分け・司会・統合報告・抜け漏れの番人", "color": "#475569", "rank": "bucho"},
    {"key": "common", "name": "全社共通", "title": "どの部にも効く知恵", "emoji": "🏢",
     "domain": "仕事の進め方・共通ノウハウ", "color": "#6b7280", "rank": "bucho"},
]

BUCHO_KEYS = [b["key"] for b in BUCHO_DEFS]

# ① project_key の明示マップ（開発システムは原則 真田）
PROJECT_MAP: dict[str, str] = {
    # 鷹司（専務＝全社統括・横断案件の裁定/完遂ログ）
    "senmu-room": "senmu", "senmu": "senmu",
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
    # 室井（現業オペ・見守り・総務）
    "ohayo-kazuko": "muroi", "kazuko_departure_watch": "muroi",
    "soumu-room": "muroi", "muroi": "muroi",
    # 九条（経営数字・財務）
    "keiei-suji": "kujo", "keiei-zaimu": "kujo",
    # 霧島（法務）
    "keiei-houmu": "kirishima",
    # 藤堂（経営管理6ラボ）
    "keiei-senryaku": "todo", "keiei-kikaku": "todo", "keiei-soshiki": "todo",
    "keiei-jinji": "todo", "keiei-risk": "todo", "keiei-tousei": "todo",
    # 鏑木（マーケティング）
    "marketing-room": "kaburagi", "lead-autogen": "kaburagi",
    # 久世（デザイン）
    "design-room": "kuze",
    # 久遠（ブランディング）
    "branding-room": "kuon",
    # 神楽（AIDX）
    "aidx-room": "kagura",
    # 早乙女（社長室）
    "hisho-room": "saotome", "hisho-shitsu": "saotome",
    # 神楽（AI・DX担当 副社長・社長対話窓口の代行/成長ループ。2026-09-26新設で未登録だったため追加）
    "aidx-shacho-daikou": "kagura",
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
    ("kaburagi", ("集客", "広告", "運用型広告", "リード獲得", "LP", "ランディングページ",
                  "CVR", "コンバージョン", "CRM", "LTV", "SEO", "SNS運用", "効果測定",
                  "キャンペーン", "流入", "リスティング")),
    ("kuze", ("UI", "UX", "デザインシステム", "ワイヤーフレーム", "プロトタイプ", "配色",
              "トンマナ設計", "画面設計", "ビジュアル", "使いやすさ", "見た目", "レイアウト")),
    ("kuon", ("ブランディング", "ブランド戦略", "パーパス", "ネーミング", "トンマナ",
              "世界観", "スローガン", "ブランドコピー", "らしさ", "ブランド一貫性")),
    ("kagura", ("生成AI活用", "AIガバナンス", "PoC設計", "DX推進", "RPA", "データ基盤",
                "AI導入", "業務自動化の設計", "ノーコード自動化")),
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


def growth_ratio(added: int, total: int) -> float | None:
    """『その期間で増えた数』が、期間前の総数に対して何%の伸びかを返す（成長率）。

    人間の直感に合う比率：「1か月前と比べて総数が何%増えたか」。
    added: 期間内に増えた件数 / total: その部長の全期間の総数。
    base = total - added = 「その期間が始まる前の総数」。
    base <= 0（その時点ではまだ知恵ゼロ）のときは None（新規＝比較対象なし）。
    増えた数は負にならないので比率は常に 0% 以上。
    """
    base = total - added
    if base <= 0:
        return None
    return round(added / base * 100, 1)


def aggregate_compare(rows: list[dict], windows: list[dict]) -> dict[str, dict]:
    """各部長について、複数の比較窓ごとに『増えた件数』を返す。

    社長の比較ビュー用（昨日より・一週間前より・1か月前と比べて）。
    windows: [{"key": "d1", "since": ISO}, ...]（since = この窓の開始。created >= since で計上）
    戻り値: 部長キー → {<key>: 件数, ...}
    比率(%)は総数が要るので route 側で growth_ratio() を使って付ける。
    rows は aggregate と同じ生データ（created_at は ISO 文字列で比較可）。
    """
    wkeys = [w["key"] for w in windows]
    cur = {k: {wk: 0 for wk in wkeys} for k in BUCHO_KEYS}
    for r in rows:
        key = classify(r.get("project_key", ""), r.get("tags"), r.get("content_head", ""))
        created = str(r.get("created_at") or "")
        for w in windows:
            if created >= w["since"]:
                cur[key][w["key"]] += 1
    return cur


def bucho_def(key: str) -> dict | None:
    for d in BUCHO_DEFS:
        if d["key"] == key:
            return d
    return None


def month_labels(now_month: str, n: int = 6) -> list[str]:
    """now_month='YYYY-MM' から過去 n ヶ月分のラベルを古い順で返す。"""
    year, month = (int(x) for x in now_month.split("-"))
    out: list[str] = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def narrative(d: dict) -> str:
    """部長詳細ページの見出しに出す、数字を解釈した一言（自動生成）。

    「量が増えた」だけでなく「ペースが落ちていないか」「使われているか」まで
    1文で答える。誇張も過小評価もせず、判定不能な時は素直に「判定できません」と書く
    （2026-09-30 社長「しっかり成長しているか確認したい」を受けて追加）。
    """
    total = d.get("total", 0)
    added = d.get("added", 0)
    growth_pct = d.get("growth_pct")
    recalls = d.get("recalls", 0)
    daily = d.get("daily") or []

    if total == 0:
        return "まだ知恵がありません（これから）。"

    parts: list[str] = []
    if growth_pct is not None:
        gp = growth_pct + 0.0  # -0.0 を 0.0 へ正規化（"+-0.0%"という壊れた表示を防ぐ）
        parts.append(f"直近の集計期間で+{added}件（{'+' if gp >= 0 else ''}{gp}%）")
    else:
        parts.append(f"直近の集計期間で+{added}件（比較対象なし＝新規）")

    # "本日" はまだ集計が終わっていないだけの0件がありうるため、末尾が0件の時だけ除外する
    # （それより前の0件日は「実際に活動が無かった日」として保持し、連続停止を見落とさない。
    #   2026-09-30 バグチェックで、旧実装は0件日を全て除外しており連続停止を見逃す
    #   ケースがあると確定したための修正）。
    trend_days = list(daily)
    if trend_days and trend_days[-1].get("added", 0) == 0:
        trend_days = trend_days[:-1]
    recent_added = [x.get("added", 0) for x in trend_days]

    if len(recent_added) >= 3 and all(v == 0 for v in recent_added[-3:]):
        parts.append("。直近3日、知恵の追加がありません。稼働を確認してください")
    elif len(recent_added) >= 2:
        nonzero = [v for v in recent_added if v > 0]
        peak = max(nonzero) if nonzero else 0
        latest = recent_added[-1]
        if peak > 0 and latest < peak * 0.5:
            drop_pct = round((peak - latest) / peak * 100, 1)
            parts.append(f"。直近日はピーク(1日{peak}件)から-{drop_pct}%とペースが落ちています。要確認")
        elif peak > 0 and latest < peak:
            parts.append(f"。直近日はピーク(1日{peak}件)より下がっていますが大きな落ち込みではありません")
        elif peak > 0 and latest >= peak:
            parts.append("。直近日もペースは落ちていません")
        # peak == 0（直近が実質ノーデータ）の場合はペースについて言及しない（誤って安心させない）

    if recalls > 0:
        parts.append(f"。実際に使われた回数は{recalls}回")
    else:
        parts.append("。まだ実際に使われた記録（recall）はありません")

    return "".join(parts) + "。"


def day_labels(now: datetime, n: int = 10) -> list[str]:
    """now から過去 n 日分の日付ラベル（YYYY-MM-DD・JST基準）を古い順で返す（当日含む）。

    now が naive の場合は既にJSTのつもりとして扱う（呼び出し側で変換しない前提のテスト互換用）。
    tz付き（UTC等）の場合はJSTへ変換してから日付を切る（2026-09-30 バグチェックで確定した
    JST早朝データが前日扱いになる不具合の修正）。
    """
    base = now.astimezone(_JST) if now.tzinfo is not None else now
    return [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def detail(
    rows: list[dict],
    key: str,
    since_iso: str,
    prev_since_iso: str,
    now_month: str,
    recent_n: int = 20,
    top_n: int = 10,
    now: datetime | None = None,
    daily_n: int = 10,
) -> dict | None:
    """1部長分の詳細（統計＋月次推移＋最近の学び＋よく使われる知恵＋仕事内訳）。

    rows: {chunk_id, project_key, tags, content_head, created_at(ISO), recall_count}
    """
    d = bucho_def(key)
    if d is None:
        return None

    mine = [
        r for r in rows
        if classify(r.get("project_key", ""), r.get("tags"), r.get("content_head", "")) == key
    ]

    total = len(mine)
    added = sum(1 for r in mine if str(r.get("created_at") or "") >= since_iso)
    added_prev = sum(
        1 for r in mine
        if prev_since_iso <= str(r.get("created_at") or "") < since_iso
    )
    recalls = sum(int(r.get("recall_count") or 0) for r in mine)
    growth_pct = round((added - added_prev) / added_prev * 100, 1) if added_prev > 0 else None

    months = month_labels(now_month)
    monthly_map = {m: 0 for m in months}
    for r in mine:
        m = str(r.get("created_at") or "")[:7]
        if m in monthly_map:
            monthly_map[m] += 1
    monthly = [{"period": m, "added": monthly_map[m]} for m in months]

    days = day_labels(now, daily_n) if now is not None else []
    daily_map = {d: 0 for d in days}
    for r in mine:
        day = _to_jst_date(str(r.get("created_at") or ""))
        if day in daily_map:
            daily_map[day] += 1
    daily = [{"period": d, "added": daily_map[d]} for d in days]

    recent = sorted(mine, key=lambda r: str(r.get("created_at") or ""), reverse=True)[:recent_n]
    top_recalled = sorted(
        (r for r in mine if int(r.get("recall_count") or 0) > 0),
        key=lambda r: int(r.get("recall_count") or 0),
        reverse=True,
    )[:top_n]

    from collections import Counter

    pj = Counter(r.get("project_key", "") for r in mine if r.get("project_key"))
    top_projects = [{"project_key": k, "count": c} for k, c in pj.most_common(5)]

    def _item(r: dict) -> dict:
        return {
            "chunk_id": r.get("chunk_id"),
            "project_key": r.get("project_key", ""),
            "preview": (r.get("content_head") or "")[:160],
            "tags": (r.get("tags") or [])[:6],
            "created_at": str(r.get("created_at") or "")[:10],
            "recall_count": int(r.get("recall_count") or 0),
        }

    result = {
        "total": total,
        "added": added,
        "added_prev": added_prev,
        "growth_pct": growth_pct,
        "recalls": recalls,
        "monthly": monthly,
        "daily": daily,
        "recent_items": [_item(r) for r in recent],
        "top_recalled": [_item(r) for r in top_recalled],
        "top_projects": top_projects,
    }
    result["narrative"] = narrative(result)
    return {**d, **result}
