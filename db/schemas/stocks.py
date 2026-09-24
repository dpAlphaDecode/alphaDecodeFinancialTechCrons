# app/db/schemas/stock.py

from pydantic import BaseModel, ConfigDict, ConfigDict
from typing import Optional

class StockBase(BaseModel):
    symbol: str
    name: str
    exchange: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    marketcap: Optional[float]
    logo_url: Optional[str]

class StockCreate(StockBase):
    pass

class StockUpdate(BaseModel):
    name: Optional[str]
    exchange: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    marketcap: Optional[float]
    logo_url: Optional[str]

class StockResponse(StockBase):
    id: int

    class Config:
        model_config = ConfigDict(from_attributes=True)
