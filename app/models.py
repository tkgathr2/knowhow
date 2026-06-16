from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, relationship, validates


class Base(DeclarativeBase):
    pass


class KbProject(Base):
    __tablename__ = "kb_projects"

    project_key = Column(String, primary_key=True)
    display_name = Column(Text)
    allow_cross_project_search = Column(Boolean, nullable=False, default=False)
    constitution_mode = Column(String, nullable=False, default="project_only")
    embedding_model = Column(String, nullable=False, default="text-embedding-3-large")
    embedding_dimensions = Column(Integer, nullable=False, default=1536)
    search_confidence_threshold = Column(Float, nullable=False, default=0.70)
    recency_half_life_days = Column(Integer, nullable=False, default=90)
    constitution_dynamic_top_m = Column(Integer, nullable=False, default=10)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    documents = relationship("KbDocument", back_populates="project", cascade="all, delete-orphan")
    sessions = relationship("KbSession", back_populates="project", cascade="all, delete-orphan")
    chunks = relationship("KbChunk", back_populates="project", cascade="all, delete-orphan")


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    doc_type = Column(String, nullable=False)
    version = Column(String, nullable=False)
    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    is_latest = Column(Boolean, nullable=False, default=False)
    checksum = Column(Text, nullable=False)
    change_log = Column(Text)
    diff_summary = Column(Text)
    processing_status = Column(String, nullable=False, default="ready")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    project = relationship("KbProject", back_populates="documents")

    __table_args__ = (
        Index("uq_kb_documents_version", "project_key", "doc_type", "version", unique=True),
    )


class KbSession(Base):
    __tablename__ = "kb_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    tool = Column(String, nullable=False)
    status = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer)
    raw_log = Column(Text, nullable=False)
    normalized_log = Column(Text, nullable=False)
    summary_json = Column(JSONB)
    summary_text = Column(Text)
    tags = Column(ARRAY(Text), nullable=False, default=list)
    error_count = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    ingest_state = Column(String, nullable=False, default="queued")
    raw_log_retention_until = Column(DateTime(timezone=True))
    hash = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project = relationship("KbProject", back_populates="sessions")
    feedback = relationship("KbFeedback", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("uq_kb_sessions_hash", "project_key", "hash", unique=True),
        Index("ix_kb_sessions_project_created", "project_key", "created_at"),
    )

    @validates("raw_log", "normalized_log", "tags")
    def _sanitize_utf8(self, key, value):
        from app.textutil import sanitize_tags, sanitize_utf8

        return sanitize_tags(value) if key == "tags" else sanitize_utf8(value)


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    source_type = Column(String, nullable=False)
    source_id = Column(BigInteger, nullable=False)
    chunk_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    token_count = Column(Integer)
    importance_score = Column(Integer, nullable=False, default=5)
    tags = Column(ARRAY(Text), nullable=False, default=list)
    meta = Column(JSONB, nullable=False, default=dict)
    embedding = Column(Vector(1536))
    embedding_model = Column(String, nullable=False, default="text-embedding-3-large")
    embedding_dimensions = Column(Integer, nullable=False, default=1536)
    helpful_count = Column(Integer, nullable=False, default=0)
    unhelpful_count = Column(Integer, nullable=False, default=0)
    alpha = Column(Float, nullable=False, default=1.0)
    beta = Column(Float, nullable=False, default=1.0)
    confidence_score = Column(Float, nullable=False, default=0.5)
    last_helpful_at = Column(DateTime(timezone=True))
    last_unhelpful_at = Column(DateTime(timezone=True))
    last_recalled_at = Column(DateTime(timezone=True))
    recall_count = Column(Integer, nullable=False, default=0)
    search_vector = Column(TSVECTOR)
    is_deprecated = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project = relationship("KbProject", back_populates="chunks")

    __table_args__ = (
        Index("ix_kb_chunks_project_created", "project_key", "created_at"),
        Index("ix_kb_chunks_source", "source_type", "source_id"),
    )

    @validates("content", "tags")
    def _sanitize_utf8(self, key, value):
        # 孤立サロゲート等の不正バイトを保存前に除去（22021事故の水際防止）
        from app.textutil import sanitize_tags, sanitize_utf8

        return sanitize_tags(value) if key == "tags" else sanitize_utf8(value)


class KbFeedback(Base):
    __tablename__ = "kb_feedback"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    session_id = Column(BigInteger, ForeignKey("kb_sessions.id", ondelete="CASCADE"), nullable=False)
    query = Column(Text, nullable=False)
    query_tags = Column(ARRAY(Text), nullable=False, default=list)
    returned_chunk_ids = Column(ARRAY(BigInteger), nullable=False)
    selected_chunk_ids = Column(ARRAY(BigInteger), nullable=False)
    resolved = Column(Boolean, nullable=False)
    was_helpful = Column(String, nullable=False)
    resolution_time_seconds = Column(Integer)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    session = relationship("KbSession", back_populates="feedback")

    __table_args__ = (
        Index("ix_kb_feedback_project_created", "project_key", "created_at"),
    )


class KbIssue(Base):
    __tablename__ = "kb_issues"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(BigInteger, ForeignKey("kb_chunks.id", ondelete="CASCADE"), nullable=False)
    reason = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_kb_issues_project_status", "project_key", "status"),
    )


class KbRecallLog(Base):
    __tablename__ = "kb_recall_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False)
    query = Column(Text, nullable=False)
    returned_chunk_ids = Column(ARRAY(BigInteger), nullable=False, default=list)
    top_score = Column(Float)
    result_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_kb_recall_log_project_created", "project_key", "created_at"),
    )


class KbTokenCutterEvent(Base):
    __tablename__ = "kb_token_cutter_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    pc = Column(Text)
    tool = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    target_kb = Column(Integer)
    est_tokens = Column(Integer, nullable=False, default=0)
    meta = Column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_tc_events_occurred", "occurred_at"),
    )


class KbAnthropicReceipt(Base):
    __tablename__ = "kb_anthropic_receipts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    receipt_no = Column(Text, nullable=False, unique=True)
    receipt_date = Column(Date, nullable=False)
    description = Column(Text, nullable=False)
    kind = Column(String, nullable=False)
    subtotal_usd = Column(Float, nullable=False)
    tax_usd = Column(Float, nullable=False, default=0)
    total_usd = Column(Float, nullable=False)
    usdjpy = Column(Float)
    total_jpy = Column(Integer)
    meta = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_anthropic_receipts_date", "receipt_date"),
    )


class KbExternalSource(Base):
    __tablename__ = "kb_external_sources"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_type = Column(String, nullable=False)
    source_url = Column(Text, nullable=False)
    project_key = Column(String, ForeignKey("kb_projects.project_key", ondelete="SET NULL"))
    config = Column(JSONB, nullable=False, default=dict)
    last_synced_at = Column(DateTime(timezone=True))
    sync_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- こえキング（録音資産化）Phase 0 ---


class KbRecording(Base):
    __tablename__ = "kb_recordings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    plaud_id = Column(Text, nullable=False, unique=True)
    title = Column(Text)
    recorded_at = Column(DateTime(timezone=True))
    duration_minutes = Column(Integer)
    transcript_status = Column(String, nullable=False, default="pending")
    speaker_set = Column(ARRAY(Text), nullable=False, default=list)
    meta = Column(JSONB, nullable=False, default=dict)
    ingested_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    utterances = relationship("KbUtterance", back_populates="recording", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_kb_recordings_recorded_at", "recorded_at"),
        Index("ix_kb_recordings_status", "transcript_status"),
    )

    @validates("speaker_set")
    def _sanitize_tags(self, key, value):
        from app.textutil import sanitize_tags

        return sanitize_tags(value)


class KbUtterance(Base):
    __tablename__ = "kb_utterances"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recording_id = Column(BigInteger, ForeignKey("kb_recordings.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False)
    speaker = Column(Text, nullable=False)
    speaker_raw = Column(Text)
    start_ms = Column(BigInteger, nullable=False)
    end_ms = Column(BigInteger, nullable=False)
    content = Column(Text, nullable=False)

    recording = relationship("KbRecording", back_populates="utterances")

    __table_args__ = (
        Index("uq_kb_utterances_seq", "recording_id", "seq", unique=True),
        Index("ix_kb_utterances_speaker", "speaker"),
    )

    @validates("content")
    def _sanitize_utf8(self, key, value):
        from app.textutil import sanitize_utf8

        return sanitize_utf8(value)


class KbSpeakerAlias(Base):
    __tablename__ = "kb_speaker_aliases"

    alias = Column(Text, primary_key=True)
    canonical = Column(Text, nullable=False)


class KbSignal(Base):
    """ロア（録音資産）から抽出した「経営判断に役立つシグナル」。

    秋好モデル（録音→まとめ→"効くものだけ"抽出）の③。日次ダイジェスト生成と同じ
    入力から LLM が「社長が知る/判断すべきこと」だけを構造化して取り出し、ここに溜める。
    雑談・確定済みは捨てる。dedup_hash で同一日の再実行による重複を冪等に防ぐ。
    """

    __tablename__ = "kb_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    project_key = Column(
        String, ForeignKey("kb_projects.project_key", ondelete="CASCADE"), nullable=False, default="lore"
    )
    # シグナルが指す対象日（JST）。録音日に紐づく。
    signal_date = Column(Date, nullable=False)
    # 種別: decision/risk/opportunity/promise/complaint/number/other
    signal_type = Column(String, nullable=False, default="other")
    title = Column(Text, nullable=False)
    detail = Column(Text)
    # 誰が/誰について（任意）
    who = Column(Text)
    importance = Column(Integer, nullable=False, default=5)
    # 対応状態: open（未対応）/done（対応済み）/dismissed（捨てた）
    status = Column(String, nullable=False, default="open")
    source_recording_id = Column(BigInteger)
    # 同一日内の重複防止キー（type+title の正規化ハッシュ）
    dedup_hash = Column(Text, nullable=False)
    meta = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_kb_signals_dedup", "project_key", "signal_date", "dedup_hash", unique=True),
        Index("ix_kb_signals_date", "signal_date"),
        Index("ix_kb_signals_type", "signal_type"),
        Index("ix_kb_signals_status", "status"),
        Index("ix_kb_signals_importance", "importance"),
    )
