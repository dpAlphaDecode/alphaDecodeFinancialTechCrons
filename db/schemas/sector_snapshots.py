# app/schemas/sector_snapshots.py

from pydantic import BaseModel, ConfigDict
from datetime import date, datetime
from typing import Optional

class SectorSnapshotBase(BaseModel):
    sector_name: str
    date: date
    marketcap: float
    calculated_rating: Optional[float] = None

class SectorSnapshotCreate(SectorSnapshotBase):
    pass

class SectorSnapshotRead(SectorSnapshotBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        model_config = ConfigDict(from_attributes=True)
