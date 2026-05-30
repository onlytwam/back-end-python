# app/saleschannel/router.py

from __future__ import annotations
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from database import get_db
from .models  import SalesChannel
from .schemas import SalesChannelCreateIn, SalesChannelUpdateIn, SalesChannelResponse  

router = APIRouter(
    prefix="/api/SalesChannels",
    tags=["Sales Channels"],
)


# ── GET all ───────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=List[SalesChannelResponse],
    summary="List all sales channels (active + inactive)",response_model_by_alias=False,
)
def list_channels(
    active_only: bool    = True,
    db:          Session = Depends(get_db),
):
    q = db.query(SalesChannel)
    if active_only:
        q = q.filter(SalesChannel.IsActive == True)
    return q.order_by(SalesChannel.SortOrder).all()


# ── GET one ───────────────────────────────────────────────────────────────────

@router.get(
    "/{channel_id}",
    response_model=SalesChannelResponse,
    summary="Get a single sales channel by ID",response_model_by_alias=False,
)
def get_channel(
    channel_id: int     = Path(..., ge=1),
    db:         Session = Depends(get_db),
):
    ch = db.query(SalesChannel).filter(SalesChannel.ChannelId == channel_id).first()
    if not ch:
        raise HTTPException(404, detail=f"Channel {channel_id} not found.")
    return ch


# ── POST create ───────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=SalesChannelResponse,
    status_code=201,
    summary="Create a new sales channel",
    response_model_by_alias=False,
)
def create_channel(
    payload: SalesChannelCreateIn,
    db:      Session = Depends(get_db),
):
    # Duplicate check
    exists = db.query(SalesChannel).filter(
        SalesChannel.ChannelCode == payload.channelCode
    ).first()
    if exists:
        raise HTTPException(
            400,
            detail=f"Channel with code '{payload.channelCode}' already exists.",
        )

    ch = SalesChannel(
        ChannelCode   = payload.channelCode,
        ChannelName   = payload.channelName,
        Emoji         = payload.emoji,
        StockSource   = payload.stockSource,
        CommissionPct = payload.commissionPct,
        IsActive      = payload.isActive,
        SortOrder     = payload.sortOrder,
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


# ── PATCH update ──────────────────────────────────────────────────────────────

@router.patch(
    "/{channel_id}",
    response_model=SalesChannelResponse,
    summary="Update a sales channel (partial update)",
    response_model_by_alias=False,
)
def update_channel(
    payload:    SalesChannelUpdateIn,
    channel_id: int     = Path(..., ge=1),
    db:         Session = Depends(get_db),
):
    ch = db.query(SalesChannel).filter(SalesChannel.ChannelId == channel_id).first()
    if not ch:
        raise HTTPException(404, detail=f"Channel {channel_id} not found.")

    # Map camelCase payload → PascalCase column
    field_map = {
        "channelName":   "ChannelName",
        "emoji":         "Emoji",
        "stockSource":   "StockSource",
        "commissionPct": "CommissionPct",
        "isActive":      "IsActive",
        "sortOrder":     "SortOrder",
    }
    for py_field, col_name in field_map.items():
        val = getattr(payload, py_field)
        if val is not None:
            setattr(ch, col_name, val)

    ch.ModifiedDate = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ch)
    return ch


# ── DELETE (soft) ─────────────────────────────────────────────────────────────

@router.delete(
    "/{channel_id}",
    status_code=204,
    summary="Deactivate a sales channel (soft delete)",
    response_model_by_alias=False
)
def deactivate_channel(
    channel_id: int     = Path(..., ge=1),
    db:         Session = Depends(get_db),
):
    ch = db.query(SalesChannel).filter(SalesChannel.ChannelId == channel_id).first()
    if not ch:
        raise HTTPException(404, detail=f"Channel {channel_id} not found.")
    if ch.ChannelCode == "POS":
        raise HTTPException(400, detail="POS channel cannot be deactivated.")

    ch.IsActive     = False
    ch.ModifiedDate = datetime.now(timezone.utc)
    db.commit()