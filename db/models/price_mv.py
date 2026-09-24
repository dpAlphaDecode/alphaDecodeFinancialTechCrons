from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, Interval, Date
from app.db.base import Base

class PriceSnapshotMV(Base):
    __tablename__ = "price_30m_snapshot_mv"

    symbol_id = Column(Integer, primary_key=True)  # <-- FIXED
    symbol = Column(String(32))
    bar_start_ts = Column(DateTime(timezone=True))
    open = Column(Numeric)
    high = Column(Numeric)
    low = Column(Numeric)
    close = Column(Numeric)
    volume = Column(Numeric)
    source = Column(Text)
    trading_date = Column(Date)
    age_interval = Column(Interval)
