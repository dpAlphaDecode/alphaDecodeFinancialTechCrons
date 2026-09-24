from sqlalchemy import Boolean, Column, Date, DateTime, Index, Integer, String, Text, func

from db.base import Base


class JobRunLog(Base):
    """Append-only audit trail: one row per scheduler job execution."""

    __tablename__ = "job_run_log"

    id = Column(Integer, primary_key=True)
    job_key = Column(String(64), nullable=False)
    run_date = Column(Date, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False)  # success / failed / no_data_due
    summary = Column(Text, nullable=True)
    error_detail = Column(Text, nullable=True)
    email_sent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_job_run_log_job_key_run_date", "job_key", "run_date"),
    )
