from sqlalchemy import Column, Integer, String, Boolean
from app.db.base import Base

class FinancialScrapePair(Base):
    __tablename__ = "financial_scrape_pairs"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False)
    quarter = Column(String(2), nullable=False)    # QY, QJ, QS, QM, QD
    type = Column(String(32), nullable=False)      # BalanceSheet, ProfitLoss, etc.
    processed = Column(Boolean, nullable=False, default=False)
