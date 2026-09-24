from sqlalchemy import (
    Column, Integer, String, Date, DateTime, func, ForeignKey, UniqueConstraint, Index
)
from db.base import Base


class FinancialCalendar(Base):
    __tablename__ = "financial_calendar"

    id = Column(Integer, primary_key=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    result_date = Column(Date, nullable=False)
    results_type = Column(String, nullable=False)
    results_period = Column(String, nullable=False)
    nature_of_report = Column(String, nullable=False)
    result_year = Column(Integer, nullable=False)

    # Set by fetch_todays_financials_and_stock_details.run() once this combo's
    # GetFinancialResults call has been attempted, so the next-morning retry
    # job knows exactly which combos still need fetching.
    fetched_at = Column(DateTime(timezone=True), nullable=True)
    fetch_status = Column(String, nullable=True)  # success / failed

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "symbol_id", "result_date", "results_type", "results_period", "nature_of_report",
            name="uq_financial_calendar_symbol_date_item",
        ),
        Index("idx_financial_calendar_symbol", "symbol_id"),
    )
