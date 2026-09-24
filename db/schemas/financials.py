from pydantic import BaseModel, ConfigDict
from datetime import date
from typing import Optional

class FinancialBase(BaseModel):
    symbol_id: int
    date: date
    metric_name: str
    metric_value: Optional[float] = None

class FinancialCreate(FinancialBase):
    pass

class FinancialOut(FinancialBase):
    id: int
    class Config:
        model_config = ConfigDict(from_attributes=True)
