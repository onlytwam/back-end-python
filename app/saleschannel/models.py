# app/saleschannel/models.py

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, Numeric, DateTime
from database import Base


class SalesChannel(Base):
    __tablename__ = "SalesChannels"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    ChannelId     = Column(Integer,        primary_key=True, index=True)
    ChannelCode   = Column(String(30),     nullable=False, unique=True)   # "POS", "Amazon"
    ChannelName   = Column(String(60),     nullable=False)
    Emoji         = Column(String(10),     nullable=True)
    StockSource   = Column(String(20),     nullable=False)                # "store" | "warehouse"
    CommissionPct = Column(Numeric(5, 2),  nullable=False, default=0)
    IsActive      = Column(Boolean,        nullable=False, default=True)
    SortOrder     = Column(Integer,        nullable=False, default=0)
    CreatedDate   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ModifiedDate  = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))