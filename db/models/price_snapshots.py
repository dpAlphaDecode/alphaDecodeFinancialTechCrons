from sqlalchemy import Column, Integer, Date, Numeric, BigInteger, DateTime, ForeignKey, func
from db.base import Base

class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False)
    date = Column(Date, nullable=False)

    open_price = Column(Numeric, nullable=True)
    close_price = Column(Numeric, nullable=True)
    high = Column(Numeric, nullable=True)
    low = Column(Numeric, nullable=True)
    volume = Column(BigInteger, nullable=True)

    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now(), onupdate=func.now())
