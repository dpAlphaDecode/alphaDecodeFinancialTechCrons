from pydantic import BaseModel, ConfigDict
from typing import Optional

class SymbolMetricsRatingOut(BaseModel):
    symbol_id: int
    symbol: str
    name: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    isin: Optional[str]

    # Metrics
    _52WeekHigh: Optional[float]
    _52WeekLow: Optional[float]
    debt_to_equity: Optional[float]
    dividend_yield: Optional[float]
    price: Optional[float]
    price_to_book: Optional[float]
    shares: Optional[float]
    volume: Optional[float]
    volume_avg_21day: Optional[float]

    # Ratings
    rating_price: Optional[float]
    rating_smr: Optional[float]
    rating_earnings: Optional[float]
    rating_sector: Optional[float]
    rating_ibr: Optional[float]

    class Config:
        model_config = ConfigDict(from_attributes=True)
