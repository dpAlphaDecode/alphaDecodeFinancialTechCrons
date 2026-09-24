from sqlalchemy import Column, Integer, ForeignKey, Date, String, Numeric, DateTime, func
from db.base import Base

class Financial(Base):
    __tablename__ = "financials"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    frequency = Column(String, nullable=False)   # ✅ Add this
    period = Column(String, nullable=True)       # ✅ And this
    report_type = Column(String(20), nullable=False)
    metric_name = Column(String, nullable=False)
    metric_value = Column(Numeric, nullable=True)
    year = Column(Integer, nullable=True)  # <-- ✅ ADD THIS LINE

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
