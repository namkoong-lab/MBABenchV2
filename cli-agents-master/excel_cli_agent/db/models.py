from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Task(Base):
    """Task table: READ-ONLY. Never insert/update/delete rows from code."""

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_name = Column(String(512), nullable=True, index=True)
    task_starting_files = Column(JSON, nullable=True)  # list of str: AWS S3 paths
    task_solution_files = Column(JSON, nullable=True)  # list of str: AWS S3 paths
    task_source = Column(String(100), nullable=True)  # 'fmwc', 'modeloff', or 'wsp'
    deprecated = Column(Boolean, nullable=True)
    deprecated_reason = Column(Text, nullable=True)
    # No old_id: the column exists in BizbenchV1 but not MBABenchV2, and a mapped
    # attribute lands in every SELECT — it broke all v2 task lookups (2026-08-12).
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True))

    # Relationships
    attempts = relationship("TaskAttempt", back_populates="task")


class TaskAttempt(Base):
    """Task Attempt: each row corresponds to an agent's attempt"""

    __tablename__ = "task_attempts"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    prompt_files = Column(JSON, nullable=False)  # list of str: S3 paths to prompts
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    agent_model_name = Column(String(512), nullable=False)
    agent_model_type = Column(String(128), nullable=False)  # 'api'
    attempt_files = Column(JSON, nullable=True)  # list of str: S3 paths to attempt files
    time_taken_min = Column(Float, nullable=False)  # time taken in minutes
    cost = Column(Float, nullable=True)  # cost of the run in USD
    agent_failed = Column(Boolean, nullable=False, default=False)
    agent_failed_reason = Column(Text, nullable=True)
    deprecated = Column(Boolean, nullable=False, default=False)
    deprecated_reason = Column(Text, nullable=True)
    prompt_version = Column(Integer, nullable=True)
    # True if any iteration ran with reduced context (sheet summarization /
    # PDF truncation). NULL: unknown, predates this column, or a pipeline
    # without the concept (GUI / coding rows).
    context_reduced = Column(Boolean, nullable=True)
    # extra_configs (JSONB) exists in MBABenchV2 only — not mapped for the
    # same reason as old_id above. AutoBatchRunner writes it with raw SQL
    # after the insert when the connected DB has the column.
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True))

    # Relationships
    task = relationship("Task", back_populates="attempts")
