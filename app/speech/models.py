from sqlalchemy import Column, Integer, String, Boolean, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.sql import func
from database import Base


class AzureSpeechConfig(Base):
    """
    Stores Azure Cognitive Services Speech credentials.
    Schema: speech  |  Table: azure_speech_config
    """
    __tablename__  = "azure_speech_config"
    __table_args__ = {"schema": "speech"}

    id               = Column(Integer, primary_key=True, autoincrement=True)
    resource_name    = Column(String(100), nullable=False)
    subscription_key = Column(String(300), nullable=False)
    region           = Column(String(50),  nullable=False, default="centralindia")
    is_active        = Column(Boolean,     nullable=False, default=True)
    notes            = Column(Text)
    created_date     = Column(TIMESTAMP(timezone=True), server_default=func.now())
    modified_date    = Column(TIMESTAMP(timezone=True), server_default=func.now(),
                              onupdate=func.now())