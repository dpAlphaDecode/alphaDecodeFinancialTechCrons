from sqlalchemy import Column, Integer, String, Date, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from db.base import Base


class SectorMetric(Base):
    __tablename__ = "sector_metrics"

    id = Column(Integer, primary_key=True, index=True)
    sector_id = Column(Integer, ForeignKey("sectors.id", ondelete="CASCADE"), nullable=False)
    metric_name = Column(String(64), nullable=False)     # e.g. price_rating, PE, etc.
    metric_value = Column(Numeric(20, 6), nullable=False)
    date = Column(Date, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationship
    sector_rel = relationship("Sector", back_populates="metrics")
    
