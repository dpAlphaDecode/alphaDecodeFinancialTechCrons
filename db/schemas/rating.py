# app/db/schemas/rating.py

from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import date

class RatingBase(BaseModel):
    symbol: str
    rating_type: str  # e.g., "Price", "Earnings", "SMR"
    score: Optional[float]
    grade: Optional[str]
    calculated_on: date

class RatingCreate(RatingBase):
    pass

class RatingResponse(RatingBase):
    id: int

    class Config:
        model_config = ConfigDict(from_attributes=True)
