from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text, Time, func

from db.base import Base


class ScheduledJob(Base):
    """
    One row per logical cron job. `run_time` and `depends_on_job_key` are the
    knobs scheduler/runner.py re-reads on every tick, so timings/ordering can
    be changed by editing this table directly -- no redeploy needed.
    """

    __tablename__ = "scheduled_jobs"

    id = Column(Integer, primary_key=True)
    job_key = Column(String(64), unique=True, nullable=False)
    description = Column(String(255), nullable=True)

    run_time = Column(Time, nullable=False)  # IST wall-clock, earliest time this job may fire
    depends_on_job_key = Column(String(64), nullable=True)  # only fires once that job succeeded today
    enabled = Column(Boolean, default=True, nullable=False)

    last_run_date = Column(Date, nullable=True)  # IST date this job last completed (any status)
    last_status = Column(String(16), nullable=True)  # success / failed / no_data_due
    last_summary = Column(Text, nullable=True)
    last_run_started_at = Column(DateTime(timezone=True), nullable=True)
    last_run_finished_at = Column(DateTime(timezone=True), nullable=True)

    notify_on_success = Column(Boolean, default=True, nullable=False)
    notify_on_failure = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
