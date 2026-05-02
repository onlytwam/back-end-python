from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime


class NewsletterSubscribe(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


class NewsletterUnsubscribe(BaseModel):
    email: str


class NewsletterBlast(BaseModel):
    subject:    Optional[str] = ""   # Optional for WhatsApp-only blast
    body_html:  str
    sent_by:    Optional[str] = None
    media_url:  Optional[str] = None          # WhatsApp image URL
    channels:   Optional[str] = "both"       # "email" | "whatsapp" | "both"


class NewsletterResponse(BaseModel):
    NewsletterId:   int
    email:          str
    isActive:       bool
    createdDate:    Optional[datetime] = None
    modifiedDate:   Optional[datetime] = None
    organizationId: Optional[int]      = None

    class Config:
        from_attributes = True