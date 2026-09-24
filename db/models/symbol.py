from sqlalchemy import Column, Integer, String, Text, DateTime, func, Boolean, ForeignKey,Float
from sqlalchemy.orm import relationship
from db.base import Base

class Symbol(Base):
    __tablename__ = "symbols"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(32), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    exchange = Column(String(16), nullable=True)
    isin = Column(String(20), unique=True, nullable=True)
    logo_url = Column(Text, nullable=True)
    series = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    archive = Column(Boolean, default=False, nullable=False)
    listed_date = Column(DateTime(timezone=True))

    # New columns
    high_52_week = Column(Float, nullable=True)
    low_52_week = Column(Float, nullable=True)

    
    # Foreign key to industries
    industry_id = Column(Integer, ForeignKey("industries.id"), nullable=True)
    sector_id = Column(Integer, ForeignKey("sectors.id"), nullable=True)


    final_status = Column(String(64), nullable=False)

    # Relationship to Industry
    industry_rel = relationship("Industry", back_populates="symbols")
    sector_rel = relationship("Sector", back_populates="symbols")

