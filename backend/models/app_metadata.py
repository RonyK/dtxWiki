from sqlalchemy import Column, String

from backend.models.base import Base


class AppMetadata(Base):
    __tablename__ = "app_metadata"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
