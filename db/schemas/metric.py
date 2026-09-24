from pydantic import BaseModel, ConfigDict, ConfigDict
from datetime import date
from typing import Optional

class MetricBase(BaseModel):
    symbol_id: int
    date: date
    metric_name: str
    metric_value: Optional[float]

class MetricCreate(MetricBase):
    pass

class MetricInDB(MetricBase):
    id: int

    class Config:
        model_config = ConfigDict(from_attributes=True)

