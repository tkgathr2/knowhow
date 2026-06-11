"""学びの自動ingest受け口。

SessionEnd フックから「セッションの抜粋」を受け取り、サーバ側で OpenAI により
「教訓・型」へ蒸留＋PIIマスクし、knowhow に学びとして memorize する。
鍵はサーバの OPENAI_API_KEY を再利用するので、フック側は KB_API_KEY だけでよい。

- 認証: require_api_key（KB_API_KEY）。フックが X-API-Key で叩く。
- 蒸留して **要約のみ** を保存（生ログ/transcript は保存しない＝PII最小化）。
- 重複は要約のhashでスキップ。OpenAI未設定・蒸留失敗・空入力は stored=false で安全に返す。
- D1: 蒸留プロンプトv2 / 2段ゲート（is_generic + passes_gate）
- D2: 初期値 α=3.0, β=1.0, confidence_score=0.75 に引き下げ
"""

from __future__ import annotations

import hashlib
import json
import re

from fastapi import APIRouter, Depends
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["auto-learn"])

_DISTILL_MODEL = "gpt-4o-mini"
_MAX_INPUT_CHARS = 18000

# ── D1: プロンプトv2 ────────────────────────────────────────────────────────
_SYSTEM = """あなたは開発の振り返り係です。渡された開発セッションの抜粋から、
再利用できる『教訓・型・落とし穴・うまくいった手順』を日本語で最大3件抽出します。

【必須要件】
1. evidence（根拠）は「セッション中に実際に起きた具体的事実」を書く。
   エラー名・コマンド・数値・ファイル名・ライブラリ名など具体物を必ず含めること。
2. applicability（適用場面）はどんな状況・場面で使えるかを書く。
3. specificity（具体度）は自己採点（5=固有の再現手順レベル、1=一般論）。
4. 固有名詞・コマンド・エラー名・数値を1つも含まない一般論は出力禁止。
   【禁止例（このような内容は出してはならない）】
   - 「エラーハンドリングを適切に行う」
   - 「テストは重要である」
   - 「環境変数の設定に注意する」
   - 「ログを確認することが大切」
   - 「コードをレビューする習慣をつける」
5. 個人情報（氏名/メール/電話/住所/口座/トークンや鍵の値）は出力せずマスクする。
6. 開発上の学びが無ければ skip=true を返す。

出力は必ず以下のJSONのみ（lessons 最大3件）:
{
  "skip": false,
  "lessons": [
    {
      "summary": "...",
      "evidence": "...",
      "applicability": "...",
      "specificity": 4,
      "tags": ["tag1", "tag2"]
    }
  ]
}"""

# ── D1: 一般論パターン（純粋関数・テスト可能）────────────────────────────
_GENERIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"(適切に|正しく|きちんと|しっかり)(行|処理|対応|設定|実装|確認|記述)",
        r"(が|は|も)(重要|大切|大事|必要|必須)(だ|です|である|。|$)",
        r"に(注意|気をつける|気を付ける)(する|こと|ください|。|$)",
        r"を(心がける|意識する|徹底する)(こと|べき|。|$)",
        r"を(確認すること|確認する習慣)",
        r"(習慣|癖)(を|に)(つける|する|なる)",
        r"(テスト|ログ|レビュー|ドキュメント)(は|が)(重要|大切|必要)",
    ]
]

# 具体トークン: 4文字以上の英数字/記号列、または「」内の固有名
_CONCRETE_TOKEN_RE = re.compile(
    r'[A-Za-z0-9_\-./]{4,}|「[^」]{2,}」'
)


def is_generic(text: str) -> bool:
    """一般論パターンに該当し、かつ具体トークンを含まない場合 True を返す。"""
    has_pattern = any(p.search(text) for p in _GENERIC_PATTERNS)
    if not has_pattern:
        return False
    has_concrete = bool(_CONCRETE_TOKEN_RE.search(text))
    return not has_concrete


def passes_gate(lesson: dict) -> tuple[bool, str]:
    """品質ゲート: specificity>=4 かつ evidence 非空 かつ not is_generic(summary) → (True, "")。"""
    summary = str(lesson.get("summary", "")).strip()
    evidence = str(lesson.get("evidence", "")).strip()
    specificity = int(lesson.get("specificity", 0))

    if specificity < 4:
        return False, f"specificity={specificity} (required>=4)"
    if not evidence:
        return False, "evidence is empty"
    if is_generic(summary):
        return False, f"summary is generic: {summary[:60]}"
    return True, ""


# ── LLMレスポンス正規化（後方互換: 旧形式フォールバック）────────────────
def _normalize_distill_response(raw: dict) -> dict:
    """
    新形式: {"skip": bool, "lessons": [...]}
    旧形式: {"skip": bool, "summary": "...", "tags": [...]} → lessons 1件に変換。
    """
    if "lessons" in raw:
        return raw

    # 旧形式フォールバック
    summary = str(raw.get("summary", "")).strip()
    if not summary:
        return {"skip": True, "lessons": []}

    specificity = 1 if is_generic(summary) else 3
    lesson = {
        "summary": summary,
        "evidence": "",   # 旧形式は evidence を持たないので空
        "applicability": "",
        "specificity": specificity,
        "tags": [str(t)[:40] for t in (raw.get("tags") or [])],
    }
    return {"skip": raw.get("skip", False), "lessons": [lesson]}


# ── OpenAI 蒸留（1コール・既存構成維持）─────────────────────────────────
async def _distill(transcript: str) -> dict | None:
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    text = transcript[-_MAX_INPUT_CHARS:]
    try:
        resp = await client.chat.completions.create(
            model=_DISTILL_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=900,
        )
        raw = json.loads(resp.choices[0].message.content)
        return _normalize_distill_response(raw)
    except Exception:
        return None


# ── リクエスト/レスポンス スキーマ ─────────────────────────────────────
class AutoLearnRequest(BaseModel):
    project_key: str = "cto-lab"
    transcript: str
    tags: list[str] = Field(default_factory=list)


class AutoLearnResponse(BaseModel):
    stored: bool
    chunk_id: int | None = None
    stored_count: int = 0
    reason: str | None = None


# ── エンドポイント ──────────────────────────────────────────────────────
@router.post("/auto-learn", response_model=AutoLearnResponse, dependencies=[Depends(require_api_key)])
async def auto_learn(
    req: AutoLearnRequest, db: AsyncSession = Depends(get_db)
) -> AutoLearnResponse:
    if not req.transcript or len(req.transcript.strip()) < 400:
        return AutoLearnResponse(stored=False, reason="too_short")

    distilled = await _distill(req.transcript)
    if not distilled:
        return AutoLearnResponse(stored=False, reason="distill_unavailable")

    lessons = distilled.get("lessons") or []
    skip_flag = distilled.get("skip", False)

    # ゲート通過 lesson を絞り込む
    passed_lessons: list[dict] = []
    skip_reasons: list[str] = []

    if skip_flag:
        skip_reasons.append("LLM returned skip=true")
    else:
        for idx, lesson in enumerate(lessons[:3]):
            ok, reason = passes_gate(lesson)
            if ok:
                passed_lessons.append(lesson)
            else:
                skip_reasons.append(f"lesson[{idx}]: {reason}")

    # 全滅時: skipped セッションとして記録（チャンクは作らない）
    if not passed_lessons:
        reason_text = "; ".join(skip_reasons) if skip_reasons else "no_lesson"
        skip_hash = "skipped:" + hashlib.sha256(
            (req.transcript[-2000]).encode("utf-8")
        ).hexdigest()

        # 既存 skipped セッションと重複しないよう hash チェック
        existing_skip = (
            await db.execute(
                select(KbSession).where(
                    KbSession.project_key == req.project_key,
                    KbSession.hash == skip_hash,
                )
            )
        ).scalar_one_or_none()
        if existing_skip:
            return AutoLearnResponse(stored=False, reason="duplicate")

        project = (
            await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
        ).scalar_one_or_none()
        if not project:
            db.add(KbProject(project_key=req.project_key, display_name=req.project_key))
            await db.flush()

        session = KbSession(
            project_key=req.project_key,
            tool="claude_code",
            status="success",
            environment="local",
            raw_log="",
            normalized_log=f"SKIPPED: {reason_text}",
            tags=["auto-ingest", "skipped"],
            hash=skip_hash,
            ingest_state="skipped",
        )
        db.add(session)
        await db.commit()
        return AutoLearnResponse(stored=False, reason=reason_text)

    # プロジェクト確保
    project = (
        await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    ).scalar_one_or_none()
    if not project:
        db.add(KbProject(project_key=req.project_key, display_name=req.project_key))
        await db.flush()

    # KbSession 作成（セッション単位で1件; 複数チャンクが source_id を共有）
    combined_summary = "\n---\n".join(
        lesson["summary"] for lesson in passed_lessons
    )[:1500]
    base_tags = ["auto-ingest", "学び", *[str(t)[:40] for t in req.tags]]
    for lesson in passed_lessons:
        for t in lesson.get("tags") or []:
            base_tags.append(str(t)[:40])
    session_tags = list(dict.fromkeys(base_tags))[:12]

    log_hash = hashlib.sha256(combined_summary.encode("utf-8")).hexdigest()
    existing = (
        await db.execute(
            select(KbSession).where(
                KbSession.project_key == req.project_key,
                KbSession.hash == log_hash,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return AutoLearnResponse(stored=False, reason="duplicate")

    session = KbSession(
        project_key=req.project_key,
        tool="claude_code",
        status="success",
        environment="local",
        raw_log=combined_summary,
        normalized_log=combined_summary,
        tags=session_tags,
        hash=log_hash,
        ingest_state="summarized",
    )
    db.add(session)
    await db.flush()

    # lesson ごとに1チャンク（最大3件・D2: α=3.0, β=1.0, conf=0.75）
    stored_chunks: list[KbChunk] = []
    for lesson in passed_lessons:
        content = (
            f"{lesson['summary']}\n"
            f"根拠: {lesson['evidence']}\n"
            f"適用: {lesson['applicability']}"
        ).strip()

        chunk_tags = list(dict.fromkeys(
            session_tags + [str(t)[:40] for t in (lesson.get("tags") or [])]
        ))[:12]

        chunk = KbChunk(
            project_key=req.project_key,
            source_type="session",
            source_id=session.id,
            chunk_type="session_log",
            content=content,
            importance_score=5,
            # D2: 初期値引き下げ（未精査知見は低めから始める）
            confidence_score=0.75,
            alpha=3.0,
            beta=1.0,
            tags=chunk_tags,
            meta={
                "source": "session-end-auto",
                "distill_v": 2,
                "specificity": lesson.get("specificity", 0),
            },
        )
        db.add(chunk)
        await db.flush()

        try:
            embedding = await create_embedding(content)
            if embedding is not None:
                chunk.embedding = embedding
                chunk.embedding_model = settings.embedding_model
                chunk.embedding_dimensions = settings.embedding_dim
            stored_chunks.append(chunk)
        except Exception:
            session.ingest_state = "failed_embedding"
            await db.commit()
            return AutoLearnResponse(stored=False, reason="failed_embedding")

    session.ingest_state = "embedded"
    await db.commit()

    first_chunk = stored_chunks[0]
    await db.refresh(first_chunk)
    return AutoLearnResponse(
        stored=True,
        chunk_id=first_chunk.id,
        stored_count=len(stored_chunks),
    )
