from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.base import Base

class IndustryMetric(Base):
    __tablename__ = "industry_metrics"

    id = Column(Integer, primary_key=True, index=True)
    industry_id = Column(Integer, ForeignKey("industries.id"), nullable=False, index=True)
    metric_name = Column(String(64), nullable=False)
    metric_value = Column(Numeric(20, 6), nullable=True)

    pe_ttm = Column(Numeric(20, 6), nullable=True)

    date = Column(Date, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("industry_id", "metric_name", "date", name="industry_metrics_unique_per_day"),
    )

    # Relationship back to Industry
    symbols = relationship("Symbol", back_populates="industry_rel")
    metrics = relationship("IndustryMetric", back_populates="industry")
