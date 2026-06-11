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
from sqlalchemy.orm import DeclarativeBase, relationship


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
