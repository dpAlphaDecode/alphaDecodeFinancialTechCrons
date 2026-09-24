from sqlalchemy import Column, Integer, Float, String, Date, DateTime, ForeignKey, func
from app.db.base import Base

class Rating(Base):
    __tablename__ = "ratings"

    symbol_id = Column(Integer, ForeignKey('symbols.id'), primary_key=True, index=True)
    earning = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    smr = Column(Float, nullable=True)
    sector = Column(Float, nullable=True)
    institutional_buying_grade = Column(String, nullable=True)

    calculated_on = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now(), nullable=False)
