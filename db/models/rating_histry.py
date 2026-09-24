from sqlalchemy import Column, Integer, ForeignKey, Date, String, Numeric, DateTime, func
from app.db.base import Base

class RatingHistory(Base):
    __tablename__ = "rating_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    rating_name = Column(String, nullable=False)
    rating_value = Column(Numeric, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
