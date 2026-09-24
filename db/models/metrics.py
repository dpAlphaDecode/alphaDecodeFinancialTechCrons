from sqlalchemy import (
    Column, Integer, String, Date, Numeric, DateTime, func, ForeignKey,
    UniqueConstraint, Index
)
from app.db.base import Base

class Metric(Base):
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True)  # PK already indexed; no need for index=True
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    metric_name = Column(String(64), nullable=False)
    metric_value = Column(Numeric(20, 6), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol_id", "metric_name", "date", name="uq_metrics_symbol_metric_date"),
        Index("ix_metrics_symbol_date", "symbol_id", "date"),
    )
