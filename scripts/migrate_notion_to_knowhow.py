"""HO-83 Phase1: Notion 3DB -> ノウハウキング(knowhow / pgvector) 移行スクリプト（主案）.

🔴🔴 実行は別バトン・社長承認後。本番DB資格情報(DATABASE_URL)と OpenAI 課金(OPENAI_API_KEY)を扱う。
このスクリプトを「書く」までが Phase1 北村の担当。**ここでは実行・本番接続・embedding API
呼び出しは一切行わない**（ファイルを作成するだけ）。

仕様: Notion「👑 ノウハウキング改良・移行仕様書 v2（HO-83）」§C-1(a)(b)(c)・§C-1.5（主案＝
一回限り移行スクリプト・psycopg2 直INSERT）・§C-1.6（α/βシード表・confidence 再計算）。

----------------------------------------------------------------------------------------
使い方（実行は承認後・別バトン）:

    set DATABASE_URL=postgresql://...        # 本番 Postgres（psycopg2 形式の URL）
    set OPENAI_API_KEY=sk-...                # embedding 生成用
    set EMBEDDING_DIM=1536                   # 必ず 1536（3072 等は中断する）

    # まずドライラン（DB に書かず、変換結果・件数・confidence 分布・閾値割れ件数を出力）
    python scripts/migrate_notion_to_knowhow.py \
        --learnings export/learnings.json \
        --devlogs   export/devlogs.json \
        --failures  export/failures.json \
        --dry-run

    # 本番投入（承認後）
    python scripts/migrate_notion_to_knowhow.py \
        --learnings export/learnings.json \
        --devlogs   export/devlogs.json \
        --failures  export/failures.json \
        [--limit N]

入力 JSON フォーマット（柔軟に拾う）:
    各ファイルは「行の配列」。各行は「Notion プロパティ名 -> 値」の dict。
    プロパティ名は日本語/英語/別名いずれでも拾えるよう、列ごとに候補キーを複数用意してある
    （_first 関数 + *_KEYS 定数）。実エクスポートのキー名が違っても *_KEYS を増やせば対応可能。

冪等性（§C-1.5）:
    meta->>'notion_learning_id' / 'notion_log_id' / 'notion_failure_id' で事前存在チェックし
    スキップ。再実行しても重複INSERTしない（Notion 無傷なので何度でも実行可）。
----------------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any

# 実行時のみ必要（import 時に落ちないよう遅延 import も検討したが、本番実行前提なので素直に import）。
# 静的解析環境で psycopg2 / openai 未インストールでも「書くだけ」なら問題にならない。
try:  # pragma: no cover - 実行環境でのみ評価
    import psycopg2
    import psycopg2.extras
except Exception:  # noqa: BLE001
    psycopg2 = None  # type: ignore[assignment]

try:  # pragma: no cover
    from openai import OpenAI
except Exception:  # noqa: BLE001
    OpenAI = None  # type: ignore[assignment]


# =========================================================================================
# 定数（仕様 §C-1 / §C-1.6 準拠）
# =========================================================================================

PROJECT_KEY_CTO_LAB = "cto-lab"
EMBEDDING_MODEL = "text-embedding-3-large"

# §C-1(a): 学びDB「種別」 -> (chunk_type, importance_score)
CHUNK_TYPE: dict[str, tuple[str, int]] = {
    "ルール (Must)": ("rule", 10),
    "ルール(Must)": ("rule", 10),
    "憲法": ("rule", 10),
    "教訓 (Should)": ("insight", 6),
    "教訓(Should)": ("insight", 6),
    "ベストプラクティス": ("insight", 6),
    "反パターン": ("anti_pattern", 7),
    "推測・仮説": ("insight", 3),
    "社長の思想": ("rule", 8),
}
DEFAULT_CHUNK_TYPE = ("insight", 5)

# §C-1.6 α/βシード表（元データに α/β が無い chunk 用）。confidence=α/(α+β)。
#   rule/憲法           : 9/1 = 0.90
#   insight(教訓/BP)    : 6/2 = 0.75
#   anti_pattern        : 5/2 = 0.714
#   それ以外            : 2/2 = 0.50（推測・仮説相当＝意図的に閾値割れ＝沈める）
SEED_AB: dict[str, tuple[float, float]] = {
    "rule": (9.0, 1.0),
    "insight": (6.0, 2.0),
    "anti_pattern": (5.0, 2.0),
}
DEFAULT_SEED_AB = (2.0, 2.0)

# §C-1(c) 失敗事例「失敗類型」-> kb_issues.reason。
# kb_issues.reason の enum 許容値（実スキーマ db/schema.sql:183 のコメント＋ app/routers/feedback.py:144
# のバリデーション）は **{stale, wrong, env_mismatch, incomplete}** の4値のみ。
# 「thinking_mistake」「verification_skip」は enum に存在しない（未確定点2）。
#   → 本スクリプトの結論: reason 列には NOT NULL を満たすため `incomplete` に寄せる（安全側）が、
#     元の失敗類型は **必ず meta.failure_type に原文保持** する（将来 enum 拡張時に復元可能）。
#     reason 列に未知値を入れると将来 enum 制約化された時に壊れるため、寄せる方を採用。
REASON_ENUM = {"stale", "wrong", "env_mismatch", "incomplete"}
FAILURE_TYPE_TO_REASON: dict[str, str] = {
    "stale": "stale",
    "古い": "stale",
    "wrong": "wrong",
    "誤り": "wrong",
    "間違い": "wrong",
    "env_mismatch": "env_mismatch",
    "環境差異": "env_mismatch",
    "環境不一致": "env_mismatch",
    "incomplete": "incomplete",
    "不完全": "incomplete",
    # enum に無い → incomplete へ寄せる（原文は meta.failure_type に保持）
    "thinking_mistake": "incomplete",
    "思考ミス": "incomplete",
    "verification_skip": "incomplete",
    "検証スキップ": "incomplete",
}
DEFAULT_REASON = "incomplete"

# §C-1(c) 重大度 -> (importance_score, meta.severity ラベル)
SEVERITY_TO_IMP: dict[str, int] = {
    "🔴 重大": 8, "🔴重大": 8, "重大": 8,
    "🟡 警告": 6, "🟡警告": 6, "警告": 6,
    "🔵 軽微": 4, "🔵軽微": 4, "軽微": 4,
}
DEFAULT_SEVERITY_IMP = 6

# §C-1(b) 実行ツール正規化
TOOL_MAP: dict[str, str] = {
    "claude code": "claude_code",
    "claude_code": "claude_code",
    "devin": "devin",
    "cursor": "cursor",
    "cowork": "cowork",
}

# §C-1(b) ステータス正規化
STATUS_MAP: dict[str, str] = {
    "完了": "success", "success": "success",
    "失敗": "fail", "fail": "fail",
}
DEFAULT_STATUS = "partial"

# 学びDB「状態」-> is_deprecated（🔴非推奨/❄凍結 -> True）
DEPRECATED_STATES = {"🔴 非推奨", "🔴非推奨", "非推奨", "❄ 凍結", "❄凍結", "凍結"}

SEARCH_CONFIDENCE_THRESHOLD = 0.70  # cto-lab 既定（沈み検出用）


# =========================================================================================
# 入力プロパティ拾い出し（キー名は実エクスポートに合わせて *_KEYS を増やせる）
# =========================================================================================

def _first(row: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """row から keys のいずれかに該当する値を返す（柔軟なキー解決）。"""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    # 大文字小文字/前後空白の揺れも吸収
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = lowered.get(str(k).strip().lower())
        if v not in (None, ""):
            return v
    return default


def _as_int(v: Any, default: int | None = None) -> int | None:
    try:
        if v in (None, ""):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float | None = None) -> float | None:
    try:
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_tags(v: Any) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def _parse_dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).replace("Z", "+00:00")
    for fmt in (None,):  # try fromisoformat first
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            break
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(v), fmt)
        except ValueError:
            continue
    return None


# 学びDB 列キー候補
L_TITLE = ["タイトル", "title", "Title", "名前", "Name"]
L_BODY = ["内容", "本文", "content", "body"]
L_KIND = ["種別", "kind", "type", "カテゴリ種別"]
L_ALPHA = ["alpha値", "alpha", "α値", "α"]
L_BETA = ["beta値", "beta", "β値", "β"]
L_RECALL = ["参照回数", "recall_count", "参照"]
L_LAST_RECALL = ["最終参照日", "last_recalled_at", "最終参照"]
L_CATEGORY = ["カテゴリ", "category", "tags", "タグ"]
L_STATE = ["状態", "status", "state"]
L_PRIORITY = ["優先度ポイント", "priority_point"]
L_BASIS = ["根拠", "source_basis"]
L_COND = ["適用条件", "applicable_condition"]
L_CREDIT = ["加点履歴", "credit_history"]
L_ID = ["学びID", "学びId", "learning_id", "id", "ID"]
L_URL = ["url", "URL", "Notion URL", "notion_url"]

# 開発ログDB 列キー候補
D_REQUEST = ["依頼内容", "request", "依頼"]
D_RESULT = ["結果サマリ", "結果", "result_summary", "summary"]
D_LEARNING = ["学び", "learning", "得られた学び"]
D_TOOL = ["実行ツール", "tool", "ツール"]
D_STATUS = ["ステータス", "status", "状態"]
D_ERR = ["エラー回数", "error_count"]
D_RETRY = ["リトライ回数", "retry_count"]
D_DURATION_MIN = ["所要時間（分）", "所要時間", "duration_min", "所要時間(分)"]
D_STARTED = ["依頼日時", "started_at", "依頼日"]
D_ENDED = ["完了日時", "ended_at", "完了日"]
D_SYSTEM = ["対象システム", "system", "対象"]
D_FEEDBACK = ["フィードバック有用度", "feedback_helpful", "有用度"]
D_REF_LEARN = ["参照した学び", "referenced_learnings", "参照学び"]
D_ID = ["依頼ID", "開発ログID", "log_id", "id", "ID"]
D_REGISTERED = ["登録日", "registered_at"]

# 失敗事例DB 列キー候補
F_TITLE = ["タイトル", "title", "名前"]
F_WHAT = ["何が起きたか", "現象", "what_happened"]
F_CAUSE = ["原因", "cause"]
F_FIX = ["対策", "fix", "対応"]
F_LESSON = ["教訓", "lesson"]
F_TYPE = ["失敗類型", "failure_type", "類型"]
F_DONE = ["対策完了", "resolved", "完了"]
F_RESOLVED_AT = ["解決日時", "closed_at", "解決日"]
F_SEVERITY = ["重大度", "severity"]
F_ENV = ["環境", "environment", "env"]
F_RECURRENCE = ["再発回数", "recurrence_count", "再発"]
F_TAGS = ["タグ", "tags"]
F_SYSTEM = ["対象システム", "system", "対象"]
F_MEMBER = ["担当", "教訓を学んだメンバー", "member", "担当者"]
F_RELATED_LOGS = ["関連開発ログ", "related_logs", "related_log_ids"]
F_ID = ["失敗ID", "failure_id", "id", "ID"]
F_OCCURRED = ["発生日", "occurred_at"]
F_REGISTERED = ["登録日", "registered_at"]

# §C-0.5 対象システム -> project_key（✅実在のみ確定。🟡要確認は候補のまま・未確定点3）。
SYSTEM_TO_PROJECT: dict[str, str] = {
    "プロレポ": "prorepo",
    "キャスト名簿くん": "cast-meibo",
    "キャスト名簿": "cast-meibo",
    "seiko": "seiko",
    "tkgathr2": "seiko",
    # 🟡 要確認（未確定点3）。確定するまで候補キーへ寄せる。
    "Indeed応募通知": "recruit",
    "Indeed": "recruit",
    "らくらく契約くん": "stepup_contract_maker",
    "ほうこちゃん": "houko",
    "ほうこ": "houko",
}
DEFAULT_DEVLOG_PROJECT = "general"


# =========================================================================================
# 変換ロジック
# =========================================================================================

def map_project_key(system: Any) -> str:
    if system in (None, ""):
        return DEFAULT_DEVLOG_PROJECT
    return SYSTEM_TO_PROJECT.get(str(system).strip(), DEFAULT_DEVLOG_PROJECT)


def seed_ab(chunk_type: str) -> tuple[float, float]:
    return SEED_AB.get(chunk_type, DEFAULT_SEED_AB)


def transform_learning(row: dict[str, Any]) -> dict[str, Any]:
    """学びDB 1行 -> kb_chunks 行 dict（§C-1(a)）。"""
    kind = _first(row, L_KIND)
    chunk_type, imp = CHUNK_TYPE.get(str(kind).strip() if kind else "", DEFAULT_CHUNK_TYPE)

    alpha = _as_float(_first(row, L_ALPHA))
    beta = _as_float(_first(row, L_BETA))
    if alpha is None or beta is None:
        s_a, s_b = seed_ab(chunk_type)
        alpha = alpha if alpha is not None else s_a
        beta = beta if beta is not None else s_b
    confidence = float(alpha / (alpha + beta)) if (alpha + beta) > 0 else 0.5

    title = _first(row, L_TITLE, "") or ""
    body = _first(row, L_BODY, "") or ""
    content = f"{title}\n{body}".strip()

    state = _first(row, L_STATE)
    is_deprecated = str(state).strip() in DEPRECATED_STATES if state else False

    notion_id = _first(row, L_ID)
    meta = {
        "priority_point": _first(row, L_PRIORITY),
        "source_basis": _first(row, L_BASIS),
        "applicable_condition": _first(row, L_COND),
        "credit_history": _first(row, L_CREDIT),
        "notion_learning_id": str(notion_id) if notion_id is not None else None,
        "notion_url": _first(row, L_URL),
        "status": state,
    }

    return {
        "project_key": PROJECT_KEY_CTO_LAB,
        "source_type": "learning",
        "source_id": _as_int(notion_id, 0) or 0,
        "chunk_type": chunk_type,
        "content": content,
        "importance_score": imp,
        "tags": _as_tags(_first(row, L_CATEGORY)),
        "meta": meta,
        "alpha": alpha,
        "beta": beta,
        "confidence_score": confidence,
        "recall_count": _as_int(_first(row, L_RECALL), 0) or 0,
        "last_recalled_at": _parse_dt(_first(row, L_LAST_RECALL)),
        "is_deprecated": is_deprecated,
        "_embed_text": content,
        "_notion_id_key": "notion_learning_id",
        "_notion_id_val": str(notion_id) if notion_id is not None else None,
    }


def transform_devlog(row: dict[str, Any]) -> dict[str, Any]:
    """開発ログDB 1行 -> kb_sessions 行 + 要約 kb_chunks 行（§C-1(b)）。"""
    request = _first(row, D_REQUEST, "") or ""
    result = _first(row, D_RESULT, "") or ""
    learning = _first(row, D_LEARNING, "") or ""
    raw_log = "\n\n".join(p for p in [request, result, learning] if p).strip()
    if not raw_log:
        raw_log = "(empty)"  # NOT NULL 対策

    tool_raw = _first(row, D_TOOL)
    tool = TOOL_MAP.get(str(tool_raw).strip().lower(), str(tool_raw).strip().lower()) if tool_raw else "devin"

    status_raw = _first(row, D_STATUS)
    status = STATUS_MAP.get(str(status_raw).strip(), DEFAULT_STATUS) if status_raw else DEFAULT_STATUS

    dur_min = _as_int(_first(row, D_DURATION_MIN))
    duration_seconds = dur_min * 60 if dur_min is not None else None

    project_key = map_project_key(_first(row, D_SYSTEM))
    notion_id = _first(row, D_ID)
    # hash = sha256(依頼内容 + 依頼ID)。UNIQUE(project_key, hash) の冪等キー。
    hash_src = f"{request}{notion_id or ''}".encode("utf-8")
    log_hash = hashlib.sha256(hash_src).hexdigest()

    # environment: 環境列が開発ログDBに無い → 本番稼働系は prod、他は local（meta に推定根拠）。
    environment = "local"

    meta = {
        "notion_log_id": str(notion_id) if notion_id is not None else None,
        "feedback_helpful": _first(row, D_FEEDBACK),
        "referenced_learning_notion_urls": _as_tags(_first(row, D_REF_LEARN)),
        "environment_basis": "no_env_column_in_notion_devlog_default_local",
        "notion_registered_at": _first(row, D_REGISTERED),
    }

    session = {
        "project_key": project_key,
        "tool": tool,
        "status": status,
        "environment": environment,
        "started_at": _parse_dt(_first(row, D_STARTED)),
        "ended_at": _parse_dt(_first(row, D_ENDED)),
        "duration_seconds": duration_seconds,
        "raw_log": raw_log,
        "normalized_log": raw_log.strip(),
        "summary_text": result or None,
        "tags": _as_tags(_first(row, D_SYSTEM)),
        "error_count": _as_int(_first(row, D_ERR), 0) or 0,
        "retry_count": _as_int(_first(row, D_RETRY), 0) or 0,
        "ingest_state": "summarized",
        "hash": log_hash,
        "meta": meta,
        "_notion_id_key": "notion_log_id",
        "_notion_id_val": str(notion_id) if notion_id is not None else None,
    }

    # 要約 chunk（source_type='session', chunk_type='summary', imp=5）。検索に乗せる。
    summary_content = "\n\n".join(p for p in [result, learning] if p).strip() or raw_log
    s_a, s_b = SEED_AB["insight"]  # 要約は insight 相当のシード（0.75）で見える側に
    summary_chunk = {
        "project_key": project_key,
        "source_type": "session",
        "chunk_type": "summary",
        "content": summary_content,
        "importance_score": 5,
        "tags": _as_tags(_first(row, D_SYSTEM)),
        "meta": dict(meta),
        "alpha": s_a,
        "beta": s_b,
        "confidence_score": float(s_a / (s_a + s_b)),
        "recall_count": 0,
        "last_recalled_at": None,
        "is_deprecated": False,
        "_embed_text": summary_content,
    }
    return {"session": session, "summary_chunk": summary_chunk}


def transform_failure(row: dict[str, Any]) -> dict[str, Any]:
    """失敗事例DB 1行 -> anti_pattern kb_chunks 行 + kb_issues 行（§C-1(c)）。"""
    title = _first(row, F_TITLE, "") or ""
    what = _first(row, F_WHAT, "") or ""
    cause = _first(row, F_CAUSE, "") or ""
    fix = _first(row, F_FIX, "") or ""
    lesson = _first(row, F_LESSON, "") or ""
    content = "\n\n".join(
        f"{label}{val}" for label, val in [
            ("", title), ("何が起きたか: ", what), ("原因: ", cause),
            ("対策: ", fix), ("教訓: ", lesson),
        ] if val
    ).strip() or title or "(empty)"

    failure_type_raw = _first(row, F_TYPE)
    failure_type = str(failure_type_raw).strip() if failure_type_raw else None
    reason = FAILURE_TYPE_TO_REASON.get(failure_type, DEFAULT_REASON) if failure_type else DEFAULT_REASON
    # 安全: reason が enum 外なら必ず DEFAULT_REASON に寄せる（NOT NULL & 将来 enum 制約対策）。
    if reason not in REASON_ENUM:
        reason = DEFAULT_REASON

    severity = _first(row, F_SEVERITY)
    imp = SEVERITY_TO_IMP.get(str(severity).strip() if severity else "", DEFAULT_SEVERITY_IMP)

    done = _first(row, F_DONE)
    is_done = bool(done) and str(done).strip() not in ("", "未", "false", "False", "0", "✗", "×")
    closed_at = _parse_dt(_first(row, F_RESOLVED_AT)) if is_done else None
    status = "closed" if is_done else "open"

    project_key = map_project_key(_first(row, F_SYSTEM))
    notion_id = _first(row, F_ID)

    # anti_pattern chunk の α/β シード（重大度連動の余地もあるが、仕様シード表＝anti_pattern 5/2）。
    a, b = SEED_AB["anti_pattern"]
    confidence = float(a / (a + b))

    meta = {
        "notion_failure_id": str(notion_id) if notion_id is not None else None,
        "failure_type": failure_type,  # 原文保持（thinking_mistake/verification_skip もここに残る）
        "severity": severity,
        "environment": _first(row, F_ENV),
        "recurrence_count": _as_int(_first(row, F_RECURRENCE), 0) or 0,  # recall_count とは別概念
        "member": _first(row, F_MEMBER),
        "related_log_ids": _as_tags(_first(row, F_RELATED_LOGS)),
        "notion_occurred_at": _first(row, F_OCCURRED),
        "notion_registered_at": _first(row, F_REGISTERED),
    }

    chunk = {
        "project_key": project_key,
        "source_type": "session",  # 失敗事例には独立 source が無いため session 系に寄せる
        "chunk_type": "anti_pattern",
        "content": content,
        "importance_score": imp,
        "tags": _as_tags(_first(row, F_TAGS)),
        "meta": meta,
        "alpha": a,
        "beta": b,
        "confidence_score": confidence,
        "recall_count": 0,
        "last_recalled_at": None,
        "is_deprecated": False,
        "_embed_text": content,
    }
    issue = {
        "project_key": project_key,
        "reason": reason,
        "status": status,
        "closed_at": closed_at,
        "_notion_id_key": "notion_failure_id",
        "_notion_id_val": str(notion_id) if notion_id is not None else None,
    }
    return {"chunk": chunk, "issue": issue}


# =========================================================================================
# embedding（実行時のみ。dim=1536 厳守）
# =========================================================================================

def make_embedder(embedding_dim: int, dry_run: bool):
    """OpenAI 同期クライアントで embedding を生成する関数を返す。dry-run では None を返す。"""
    if dry_run:
        return lambda _text: None
    if OpenAI is None:
        raise RuntimeError("openai パッケージが見つかりません（実行環境に install してください）")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です")
    client = OpenAI(api_key=api_key)

    def _embed(text_value: str) -> list[float] | None:
        if not text_value:
            return None
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL, input=text_value, dimensions=embedding_dim
        )
        return resp.data[0].embedding

    return _embed


def vec_literal(emb: list[float]) -> str:
    """pgvector 用 '[...]' リテラル（devin.py の _recall_vec_literal と同形）。"""
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


# =========================================================================================
# DB I/O（psycopg2・トランザクション・冪等）
# =========================================================================================

def _exists_chunk_by_meta(cur, key: str, value: str) -> bool:
    cur.execute(
        "SELECT 1 FROM kb_chunks WHERE meta->>%s = %s LIMIT 1", (key, value)
    )
    return cur.fetchone() is not None


def _exists_session_by_meta(cur, key: str, value: str) -> bool:
    cur.execute(
        "SELECT 1 FROM kb_sessions WHERE meta->>%s = %s LIMIT 1", (key, value)
    )
    return cur.fetchone() is not None


def _exists_issue_by_meta(cur, key: str, value: str) -> bool:
    # kb_issues に meta 列は無い → 対応する anti_pattern chunk の meta で存在判定する。
    cur.execute(
        "SELECT 1 FROM kb_chunks WHERE meta->>%s = %s AND chunk_type='anti_pattern' LIMIT 1",
        (key, value),
    )
    return cur.fetchone() is not None


def _ensure_project(cur, project_key: str) -> None:
    cur.execute(
        "INSERT INTO kb_projects (project_key, display_name) VALUES (%s, %s) "
        "ON CONFLICT (project_key) DO NOTHING",
        (project_key, project_key),
    )


def _insert_chunk(cur, c: dict[str, Any], embedding: list[float] | None) -> int:
    cur.execute(
        """
        INSERT INTO kb_chunks
          (project_key, source_type, source_id, chunk_type, content, importance_score,
           tags, meta, embedding, embedding_model, embedding_dimensions,
           alpha, beta, confidence_score, recall_count, last_recalled_at, is_deprecated)
        VALUES
          (%(project_key)s, %(source_type)s, %(source_id)s, %(chunk_type)s, %(content)s,
           %(importance_score)s, %(tags)s, %(meta)s, %(embedding)s, %(embedding_model)s,
           %(embedding_dimensions)s, %(alpha)s, %(beta)s, %(confidence_score)s,
           %(recall_count)s, %(last_recalled_at)s, %(is_deprecated)s)
        RETURNING id
        """,
        {
            "project_key": c["project_key"],
            "source_type": c["source_type"],
            "source_id": c.get("source_id", 0) or 0,
            "chunk_type": c["chunk_type"],
            "content": c["content"],
            "importance_score": c["importance_score"],
            "tags": c["tags"],
            "meta": json.dumps(c["meta"], ensure_ascii=False, default=str),
            "embedding": vec_literal(embedding) if embedding is not None else None,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": len(embedding) if embedding is not None else 1536,
            "alpha": c["alpha"],
            "beta": c["beta"],
            "confidence_score": c["confidence_score"],
            "recall_count": c["recall_count"],
            "last_recalled_at": c["last_recalled_at"],
            "is_deprecated": c["is_deprecated"],
        },
    )
    return int(cur.fetchone()[0])


def _insert_session(cur, s: dict[str, Any]) -> int:
    cur.execute(
        """
        INSERT INTO kb_sessions
          (project_key, tool, status, environment, started_at, ended_at, duration_seconds,
           raw_log, normalized_log, summary_text, tags, error_count, retry_count,
           ingest_state, hash, meta)
        VALUES
          (%(project_key)s, %(tool)s, %(status)s, %(environment)s, %(started_at)s, %(ended_at)s,
           %(duration_seconds)s, %(raw_log)s, %(normalized_log)s, %(summary_text)s, %(tags)s,
           %(error_count)s, %(retry_count)s, %(ingest_state)s, %(hash)s, %(meta)s)
        ON CONFLICT (project_key, hash) DO NOTHING
        RETURNING id
        """,
        {
            **{k: s[k] for k in (
                "project_key", "tool", "status", "environment", "started_at", "ended_at",
                "duration_seconds", "raw_log", "normalized_log", "summary_text", "tags",
                "error_count", "retry_count", "ingest_state", "hash",
            )},
            "meta": json.dumps(s["meta"], ensure_ascii=False, default=str),
        },
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _insert_issue(cur, issue: dict[str, Any], chunk_id: int) -> int:
    cur.execute(
        """
        INSERT INTO kb_issues (project_key, chunk_id, reason, status, closed_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (issue["project_key"], chunk_id, issue["reason"], issue["status"], issue["closed_at"]),
    )
    return int(cur.fetchone()[0])


# =========================================================================================
# 実行
# =========================================================================================

def load_json(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        # {"results": [...]} や {"rows": [...]} のラップも吸収
        for key in ("results", "rows", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    return []


def confidence_distribution(confidences: list[float]) -> str:
    if not confidences:
        return "(no chunks)"
    buckets = Counter()
    for c in confidences:
        if c < 0.50:
            buckets["<0.50"] += 1
        elif c < 0.70:
            buckets["0.50-0.69"] += 1
        elif c < 0.80:
            buckets["0.70-0.79"] += 1
        elif c < 0.90:
            buckets["0.80-0.89"] += 1
        else:
            buckets[">=0.90"] += 1
    order = ["<0.50", "0.50-0.69", "0.70-0.79", "0.80-0.89", ">=0.90"]
    return ", ".join(f"{k}={buckets.get(k, 0)}" for k in order)


def main() -> int:
    ap = argparse.ArgumentParser(description="HO-83 Phase1 Notion 3DB -> knowhow 移行（主案）")
    ap.add_argument("--learnings", help="学びDB エクスポート JSON")
    ap.add_argument("--devlogs", help="開発ログDB エクスポート JSON")
    ap.add_argument("--failures", help="失敗事例DB エクスポート JSON")
    ap.add_argument("--dry-run", action="store_true", help="DB に書かず変換結果と統計のみ出力")
    ap.add_argument("--limit", type=int, default=None, help="各DBの先頭 N 件のみ処理")
    args = ap.parse_args()

    embedding_dim = _as_int(os.environ.get("EMBEDDING_DIM", "1536"), 1536) or 1536
    # dim=1536 厳守（3072 等は vector(1536) 列に入らず NULL 化＝検索に乗らない事故）。
    if embedding_dim != 1536:
        print(f"❌ EMBEDDING_DIM={embedding_dim} は不正。kb_chunks.embedding は vector(1536) 固定。"
              f"1536 にして再実行してください。中断します。", file=sys.stderr)
        return 2

    learnings = load_json(args.learnings)
    devlogs = load_json(args.devlogs)
    failures = load_json(args.failures)
    if args.limit is not None:
        learnings = learnings[: args.limit]
        devlogs = devlogs[: args.limit]
        failures = failures[: args.limit]

    embed = make_embedder(embedding_dim, args.dry_run)

    # ---- 変換（DB 非依存） ----
    learn_rows = [transform_learning(r) for r in learnings]
    devlog_rows = [transform_devlog(r) for r in devlogs]
    failure_rows = [transform_failure(r) for r in failures]

    all_confidences: list[float] = []
    all_confidences += [r["confidence_score"] for r in learn_rows]
    all_confidences += [r["summary_chunk"]["confidence_score"] for r in devlog_rows]
    all_confidences += [r["chunk"]["confidence_score"] for r in failure_rows]
    below_threshold = sum(1 for c in all_confidences if c < SEARCH_CONFIDENCE_THRESHOLD)

    print("=" * 78)
    print(f"HO-83 Phase1 移行 {'[DRY-RUN]' if args.dry_run else '[LIVE 🔴本番DB書込]'}")
    print(f"  学びDB        : {len(learn_rows)} 行 -> kb_chunks(learning)")
    print(f"  開発ログDB    : {len(devlog_rows)} 行 -> kb_sessions + 要約chunk")
    print(f"  失敗事例DB    : {len(failure_rows)} 行 -> anti_pattern chunk + kb_issues")
    print(f"  EMBEDDING_DIM : {embedding_dim}")
    print(f"  confidence 分布: {confidence_distribution(all_confidences)}")
    print(f"  閾値割れ(<{SEARCH_CONFIDENCE_THRESHOLD}): {below_threshold} 件（移行後に検索で沈む懸念）")
    print("=" * 78)

    if args.dry_run:
        # 代表サンプルを表示
        if learn_rows:
            s = learn_rows[0]
            print(f"[sample learning] type={s['chunk_type']} imp={s['importance_score']} "
                  f"conf={s['confidence_score']:.3f} tags={s['tags']}")
        if devlog_rows:
            s = devlog_rows[0]["session"]
            print(f"[sample devlog] project={s['project_key']} tool={s['tool']} status={s['status']} "
                  f"dur={s['duration_seconds']} hash={s['hash'][:12]}…")
        if failure_rows:
            f = failure_rows[0]
            print(f"[sample failure] reason={f['issue']['reason']} status={f['issue']['status']} "
                  f"failure_type(meta)={f['chunk']['meta']['failure_type']} imp={f['chunk']['importance_score']}")
        print("DRY-RUN 完了（DB 未接続・embedding 未生成）。")
        return 0

    # ---- LIVE: psycopg2 トランザクション ----
    if psycopg2 is None:
        print("❌ psycopg2 が見つかりません。実行環境に install してください。", file=sys.stderr)
        return 2
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("❌ DATABASE_URL が未設定です。中断します。", file=sys.stderr)
        return 2

    inserted = Counter()
    skipped = Counter()
    embed_null = 0

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # (a) 学びDB
            for c in learn_rows:
                _ensure_project(cur, c["project_key"])
                if c["_notion_id_val"] and _exists_chunk_by_meta(cur, c["_notion_id_key"], c["_notion_id_val"]):
                    skipped["learning"] += 1
                    continue
                emb = embed(c["_embed_text"])
                if emb is None:
                    embed_null += 1
                _insert_chunk(cur, c, emb)
                inserted["learning"] += 1

            # (b) 開発ログDB
            for d in devlog_rows:
                s = d["session"]
                sc = d["summary_chunk"]
                _ensure_project(cur, s["project_key"])
                if s["_notion_id_val"] and _exists_session_by_meta(cur, s["_notion_id_key"], s["_notion_id_val"]):
                    skipped["session"] += 1
                    continue
                session_id = _insert_session(cur, s)
                if session_id == 0:
                    skipped["session"] += 1  # hash 衝突でスキップ
                    continue
                inserted["session"] += 1
                sc["source_id"] = session_id
                emb = embed(sc["_embed_text"])
                if emb is None:
                    embed_null += 1
                _insert_chunk(cur, sc, emb)
                inserted["summary_chunk"] += 1

            # (c) 失敗事例DB
            for f in failure_rows:
                chunk = f["chunk"]
                issue = f["issue"]
                _ensure_project(cur, chunk["project_key"])
                if issue["_notion_id_val"] and _exists_issue_by_meta(cur, issue["_notion_id_key"], issue["_notion_id_val"]):
                    skipped["issue"] += 1
                    continue
                chunk["source_id"] = 0  # 失敗事例由来は独立 source なし
                emb = embed(chunk["_embed_text"])
                if emb is None:
                    embed_null += 1
                chunk_id = _insert_chunk(cur, chunk, emb)
                inserted["anti_pattern_chunk"] += 1
                _insert_issue(cur, issue, chunk_id)
                inserted["issue"] += 1

        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"❌ エラー発生・ロールバックしました: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("-" * 78)
    print("INSERT サマリ:", dict(inserted))
    print("SKIP   サマリ:", dict(skipped))
    print(f"embedding NULL 件数: {embed_null}（0 が理想。0 以外なら次元/APIキーを確認）")
    print("完了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
