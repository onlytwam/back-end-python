from pydantic import BaseModel
from typing import Optional


class PageImageResponse(BaseModel):
    pageKey: str
    imageUrl: str
    description: Optional[str] = None
    isActive: bool = True

    class Config:
        from_attributes = True


class PageImagesListResponse(BaseModel):
    images: list[PageImageResponse]


class UploadPageImageResponse(BaseModel):
    success: bool
    pageKey: str
    imageUrl: str


class DeletePageImageResponse(BaseModel):
    success: bool
    pageKey: str


class VisibilityUpdateRequest(BaseModel):
    isActive: bool


class VisibilityUpdateResponse(BaseModel):
    success: bool
    pageKey: str
    isActive: bool