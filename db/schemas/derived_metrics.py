from pydantic import BaseModel, ConfigDict
from datetime import date
from typing import Optional

class DerivedMetricBase(BaseModel):
    symbol_id: int
    date: date
    metric_name: str
    metric_value: Optional[float] = None

class DerivedMetricCreate(DerivedMetricBase):
    pass

class DerivedMetricOut(DerivedMetricBase):
    id: int
    class Config:
        model_config = ConfigDict(from_attributes=True)
