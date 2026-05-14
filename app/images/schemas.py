from pydantic import BaseModel
from typing import Optional


class PageImageResponse(BaseModel):
    pageKey: str
    imageUrl: str
    description: Optional[str] = None

    class Config:
        from_attributes = True


class PageImagesListResponse(BaseModel):
    images: list[PageImageResponse]


class UploadPageImageResponse(BaseModel):
    success: bool
    pageKey: str
    imageUrl: str
