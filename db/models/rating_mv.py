from sqlalchemy import Column, Integer, String, Numeric
from app.db.base import Base

class SymbolMetricsRatingMV(Base):
    __tablename__ = "symbol_metrics_ratings_mv"

    symbol_id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String)
    name = Column(String)
    sector = Column(String)
    industry = Column(String)
    isin = Column(String)
    final_status = Column(String)  # ✅ Added new column here


    _52WeekHigh = Column("52WeekHigh", Numeric)
    _52WeekLow = Column("52WeekLow", Numeric)
    debt_to_equity = Column(Numeric)
    dividend_yield = Column(Numeric)
    price = Column(Numeric)
    price_to_book = Column(Numeric)
    shares = Column(Numeric)
    volume = Column(Numeric)
    volume_avg_21day = Column(Numeric)

    rating_price = Column(Numeric)
    rating_smr = Column(Numeric)
    rating_earnings = Column(Numeric)
    rating_sector = Column(Numeric)
    rating_ibr = Column(Numeric)
