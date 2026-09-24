from sqlalchemy import Column, Integer, String, Text, DateTime, func,Numeric
from sqlalchemy.orm import relationship
from db.base import Base


class Industry(Base):
    __tablename__ = "industries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), unique=True, nullable=False)
    sector = Column(String(128), nullable=True)
    description = Column(Text, nullable=True)

    pe_ttm = Column(Numeric(20, 6), nullable=True)


    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    symbols = relationship("Symbol", back_populates="industry_rel")
