# app/db/models/price.py
from datetime import datetime
from sqlalchemy import (
    Column, Integer, Numeric, DateTime, ForeignKey,
    PrimaryKeyConstraint, Index
)
from app.db.base import Base  # your declarative Base

class Price(Base):
    __tablename__ = "price"
    __table_args__ = (
        # composite PK
        PrimaryKeyConstraint("symbol_id", "bar_start", name="pk_price_symbol_time"),
        # FK to symbols
        {"schema": "public"},
    )

    symbol_id = Column(Integer, ForeignKey("symbols.id"), nullable=False)
    bar_start = Column(DateTime(timezone=True), nullable=False)  # 15-min candle start (UTC recommended)

    open  = Column(Numeric)
    high  = Column(Numeric)
    low   = Column(Numeric)
    close = Column(Numeric)
    volume = Column(Numeric)

# ---------- Indexes ----------
# Fast per-symbol latest lookup (desc on bar_start)
Index(
    "ix_price_symbol_barstart_desc",
    Price.symbol_id, Price.bar_start.desc(),
    postgresql_using="btree",
    postgresql_where=None,   # keep all rows
)

# Optional covering index (PG 11+) to enable index-only scans
Index(
    "ix_price_symbol_barstart_desc_cover",
    Price.symbol_id, Price.bar_start.desc(),
    postgresql_include=("open", "high", "low", "close", "volume"),
)
