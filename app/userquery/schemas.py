from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


class UserQueryCreate(BaseModel):
    message: str
    attachmentUrl: Optional[str] = None


class UserQueryReply(BaseModel):
    replyText: str
    sendEmail: bool = True
    sendSms: bool = False


class UserQueryResponse(BaseModel):
    queryId: int
    message: Optional[str] = None
    attachmentUrl: Optional[str] = None
    userProfileId: Optional[str] = None
    userName: Optional[str] = None
    userEmail: Optional[str] = None
    userPhone: Optional[str] = None
    state: Optional[str] = None
    replyText: Optional[str] = None
    repliedAt: Optional[datetime] = None
    repliedBy: Optional[str] = None
    createdDate: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserQueryListResponse(BaseModel):
    count: int
    list: List[UserQueryResponse]
    parameters: Optional[Any] = None
