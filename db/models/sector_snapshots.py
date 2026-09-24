# app/db/models/sector_snapshots.py

from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, func
from app.db.base import Base

class SectorSnapshot(Base):
    __tablename__ = "sector_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    sector_name = Column(String(100), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    marketcap = Column(Numeric, nullable=False)
    calculated_rating = Column(Numeric, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=False), onupdate=func.now(), server_default=func.now(), nullable=False)
