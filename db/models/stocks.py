# app/db/models/stock.py

from sqlalchemy import Column, Integer, String, Float
from app.db.base import Base

class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    exchange = Column(String, nullable=True)
    sector = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    marketcap = Column(Float, nullable=True)
    logo_url = Column(String, nullable=True)
