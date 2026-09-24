from sqlalchemy import Column, Integer, ForeignKey, Date, String, DateTime, func
from app.db.base import Base

class MissingFinancial(Base):
    __tablename__ = "missing_financials"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    frequency = Column(String, nullable=False)
    period = Column(String, nullable=True)
    metric_name = Column(String, nullable=False)
    year = Column(Integer, nullable=False)  # ✅ Newly added
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
