from pydantic import BaseModel, ConfigDict, ConfigDict
from typing import Optional

class FinancialScrapePairBase(BaseModel):
    year: int
    quarter: str  # e.g., "QY", "QJ", "QS", "QM", "QD"
    type: str     # e.g., "BalanceSheet", "ProfitLoss"
    processed: bool = False

class FinancialScrapePairCreate(FinancialScrapePairBase):
    pass

class FinancialScrapePairUpdate(FinancialScrapePairBase):
    pass

class FinancialScrapePairInDBBase(FinancialScrapePairBase):
    id: int

    class Config:
        model_config = ConfigDict(from_attributes=True)

class FinancialScrapePair(FinancialScrapePairInDBBase):
    pass
