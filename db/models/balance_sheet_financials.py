from sqlalchemy import (
    Column, Integer, String, Date, Numeric, DateTime, func, ForeignKey,
    UniqueConstraint, Index
)
from db.base import Base


class BalanceSheetFinancial(Base):
    __tablename__ = "balance_sheet_financials"

    id = Column(Integer, primary_key=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    statement_type = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    frequency = Column(String(20), nullable=False)
    period = Column(String, nullable=True)
    metric_name = Column(String, nullable=False)
    metric_value = Column(Numeric, nullable=True)
    year = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "symbol_id", "statement_type", "frequency", "date", "metric_name",
            name="uq_balance_sheet_financials",
        ),
        Index("idx_bs_lookup", "symbol_id", "statement_type", "frequency", "date"),
    )
