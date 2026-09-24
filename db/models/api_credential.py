from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db.base import Base
import uuid


class ApiCredential(Base):
    """Per-consumer identity + AES material, provisioned directly in the DB.

    aes_key: 32 bytes, hex-encoded (64 chars) -> AES-256 key.
    aes_iv:  16 bytes, hex-encoded (32 chars) -> fixed IV, reused for every
             request/response for this username (not regenerated per call).
    """

    # NOTE: named aes_credentials (not api_credentials) — that name is
    # already taken by an unrelated, pre-existing table in this DB
    # (name/api_key/secret_key_encrypted/revoked_at columns) that nothing
    # in this codebase manages.
    __tablename__ = "aes_credentials"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, nullable=False, index=True)
    aes_key = Column(String(64), nullable=False)
    aes_iv = Column(String(32), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
