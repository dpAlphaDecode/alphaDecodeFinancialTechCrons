from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    func
)

from sqlalchemy.orm import relationship

from app.db.base import Base


# =========================================================
# BASE MIXIN
# =========================================================

class FinancialMixin:

    id = Column(Integer, primary_key=True, index=True)

    symbol_id = Column(
        Integer,
        ForeignKey("symbols.id"),
        nullable=False,
        index=True
    )

    # consolidated / standalone
    statement_type = Column(
        String(32),
        nullable=False,
        index=True
    )

    # quarterly / yearly
    frequency = Column(
        String(32),
        nullable=False,
        index=True
    )

    # Q1/Q2/Q3/Q4/FY
    period = Column(
        String(16),
        nullable=False
    )

    date = Column(
        Date,
        nullable=False,
        index=True
    )

    # financial year
    year = Column(
        Integer,
        nullable=False,
        index=True
    )

    metric_name = Column(
        String(255),
        nullable=False,
        index=True
    )

    metric_value = Column(
        Float,
        nullable=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )


# =========================================================
# PNL
# =========================================================

class PNLFinancial(Base, FinancialMixin):

    __tablename__ = "pnl_financials"

    __table_args__ = (

        UniqueConstraint(
            "symbol_id",
            "statement_type",
            "frequency",
            "period",
            "metric_name",
            "date",
            name="uq_pnl_financial"
        ),

        Index(
            "idx_pnl_lookup",
            "symbol_id",
            "statement_type",
            "frequency",
            "date"
        ),
    )

    symbol = relationship("Symbol")


# =========================================================
# BALANCE SHEET
# =========================================================

class BalanceSheetFinancial(Base, FinancialMixin):

    __tablename__ = "balance_sheet_financials"

    __table_args__ = (

        UniqueConstraint(
            "symbol_id",
            "statement_type",
            "frequency",
            "period",
            "metric_name",
            "date",
            name="uq_balance_sheet_financial"
        ),

        Index(
            "idx_balance_sheet_lookup",
            "symbol_id",
            "statement_type",
            "frequency",
            "date"
        ),
    )

    symbol = relationship("Symbol")


# =========================================================
# CASH FLOW
# =========================================================

class CashFlowFinancial(Base, FinancialMixin):

    __tablename__ = "cashflow_financials"

    __table_args__ = (

        UniqueConstraint(
            "symbol_id",
            "statement_type",
            "frequency",
            "period",
            "metric_name",
            "date",
            name="uq_cashflow_financial"
        ),

        Index(
            "idx_cashflow_lookup",
            "symbol_id",
            "statement_type",
            "frequency",
            "date"
        ),
    )

    symbol = relationship("Symbol")