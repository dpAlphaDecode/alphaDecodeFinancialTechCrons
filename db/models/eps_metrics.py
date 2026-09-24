from sqlalchemy import Column, Integer, ForeignKey, Date, Numeric, DateTime, func
from app.db.base import Base


class EPSMetrics(Base):
    __tablename__ = "eps_metrics"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)

    eps_quarterly = Column(Numeric(20, 6), nullable=True)
    eps_annual = Column(Numeric(20, 6), nullable=True)
    yoy_growth = Column(Numeric(20, 6), nullable=True)
    qoq_growth = Column(Numeric(20, 6), nullable=True)
    yoy_avg_growth = Column(Numeric(20, 6), nullable=True)
    qoq_avg_growth = Column(Numeric(20, 6), nullable=True)
    weighted_avg = Column(Numeric(20, 6), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
