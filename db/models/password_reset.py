from sqlalchemy import Column, String, DateTime
from app.db.base import Base

class PasswordReset(Base):
    __tablename__ = "password_reset_tokens"

    email = Column(String, primary_key=True)
    token = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
