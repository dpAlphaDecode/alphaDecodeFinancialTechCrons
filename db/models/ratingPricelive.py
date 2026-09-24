from sqlalchemy import Column, Integer, String, Numeric, DateTime
from app.db.base import Base
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List
from datetime import datetime
class SymbolMetricsRatingsLiveV(Base):
    __tablename__ = "symbol_metrics_ratings_live_v"

    symbol_id = Column(Integer, primary_key=True)
    symbol = Column(String)
    name = Column(String)
    sector = Column(String)
    industry = Column(String)
    isin = Column(String)
    final_status = Column(String)

    _52WeekHigh = Column("52WeekHigh", Numeric)   # ✅ still exists
    debt_to_equity = Column(Numeric)
    dividend_yield = Column(Numeric)
    shares = Column(Numeric)
    volume_avg_21day = Column(Numeric)
    book_value_per_share = Column(Numeric)
    ttm_eps = Column(Numeric)
    yoy_avg_growth = Column(Numeric)

    rating_price = Column(Numeric)
    rating_smr = Column(Numeric)
    rating_earnings = Column(Numeric)
    rating_sector = Column(Numeric)
    rating_institutional_buying = Column(String)  # looks like "C"/"E" → text

    # from price_30m_snapshot_mv
    bar_start_ts = Column(DateTime(timezone=True))
    open = Column(Numeric)
    high = Column(Numeric)
    low = Column(Numeric)
    close = Column(Numeric)
    volume = Column(Numeric)
    trading_date = Column(DateTime(timezone=False))  # might also be Date
    source = Column(String)


from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from datetime import datetime

class SymbolMetricsResponse(BaseModel):
    symbol_id: int
    symbol: Optional[str] = None
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    isin: Optional[str] = None
    final_status: Optional[str] = None

    week_52_high: Optional[float] = Field(None, alias="_52WeekHigh")
    debt_to_equity: Optional[float] = None
    dividend_yield: Optional[float] = None
    shares: Optional[float] = None
    volume_avg_21day: Optional[float] = None
    book_value_per_share: Optional[float] = None
    ttm_eps: Optional[float] = None
    yoy_avg_growth: Optional[float] = None

    rating_price: Optional[float] = None
    rating_smr: Optional[float] = None
    rating_earnings: Optional[float] = None
    rating_sector: Optional[float] = None
    rating_institutional_buying: Optional[str] = None

    bar_start_ts: Optional[datetime] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    trading_date: Optional[datetime] = None
    source: Optional[str] = None

    class Config:
        from_attributes = True

class PaginatedData(BaseModel):
    total: int
    skip: int
    limit: int
    has_more: bool
    data: List[SymbolMetricsResponse]  # Use the Pydantic model here

class PaginatedResponse(BaseModel):
    status: str
    data: PaginatedData
