from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

class SymbolBase(BaseModel):
    symbol: str = Field(..., max_length=32)
    name: str = Field(..., max_length=128)
    sector: Optional[str] = Field(None, max_length=64)
    industry: Optional[str] = Field(None, max_length=64)
    exchange: Optional[str] = Field(None, max_length=16)
    isin: Optional[str] = Field(None, max_length=20)
    logo_url: Optional[str] = None
