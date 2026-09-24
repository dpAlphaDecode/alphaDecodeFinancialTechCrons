from sqlalchemy import Column, Integer, ForeignKey, Date, Numeric, String, DateTime, func
from app.db.base import Base

class SMRMetrics(Base):
    __tablename__ = "smr_metrics"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)

    qoq_sales_growth = Column(Numeric(20, 6), nullable=True)
    margin = Column(Numeric(20, 6), nullable=True)
    roe = Column(Numeric(20, 6), nullable=True)
    weighted_avg = Column(Numeric(20, 6), nullable=True)
    smr_rating = Column(String(2), nullable=True)  # A/B/C/D/E

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
