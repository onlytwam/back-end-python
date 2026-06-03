import os

from typing import Optional, Annotated



from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException, Query

from sqlalchemy.orm import Session



from app.shared.azure_storage import upload_image, delete_image
from app.shared.url_helpers import resolve_url

from database import get_db

from . import repository, schemas



router = APIRouter(

    prefix="/api/Blog",

    tags=["Blog"]

)





# ---------------- GET ALL (with optional pagination params) ----------------

@router.get("", response_model=schemas.BlogListResponse)   # "" matches /api/Blog AND /api/Blog/

@router.get("/", response_model=schemas.BlogListResponse, include_in_schema=False)

def get_all_blogs(

    db: Session = Depends(get_db),

    # Accept the pagination/ordering params the frontend sends (used for future filtering)

    page_index: int  = Query(default=1,            alias="Page.Index"),

    page_size:  int  = Query(default=15,           alias="Page.Size"),

    order_prop: str  = Query(default="CreatedDate", alias="Order.Property"),

    order_asc:  bool = Query(default=False,         alias="Order.Ascending"),

):

    blogs = repository.get_all_blogs(db)



    # Basic in-Python ordering by createdDate (swap for DB-level ordering if preferred)

    if order_prop.lower() in ("createddate", "created_date"):

        blogs = sorted(

            blogs,

            key=lambda b: getattr(b, "createdDate", None) or getattr(b, "created_date", None) or "",

            reverse=not order_asc,

        )



    total = len(blogs)



    # Paginate

    start = (page_index - 1) * page_size

    end   = start + page_size

    paged = blogs[start:end]



    return {

        "count": total,

        "list": [
            {**b.__dict__, "imageUrl": resolve_url(b.imageUrl)}
            for b in paged
        ],

        "parameters": {

            "filters": [],

            "order": {"property": order_prop, "ascending": order_asc},

            "page":  {"index": page_index, "size": page_size},

        }

    }





# ---------------- CREATE ----------------

@router.post("/", response_model=schemas.BlogWrappedResponse)

def create_blog(

    blogTitle:   Annotated[str, Form(...)],

    blogContent: Annotated[str, Form(...)],

    description: Annotated[str, Form(...)],

    image:       Annotated[UploadFile, File(...)],

    db: Session = Depends(get_db)

):

    # Upload image to Azure Blob (or local disk in dev) — container: blogs

    image_url = upload_image(image, container_key="blogs")



    blog_data = schemas.BlogCreate(

        blogTitle=blogTitle,

        blogContent=blogContent,

        description=description,

        imageUrl=image_url

    )



    created_blog = repository.create_blog(db, blog_data)



    return {

        "success": True,

        "message": "Blog created successfully",

        "data": {**created_blog.__dict__, "imageUrl": resolve_url(created_blog.imageUrl)},

    }





# ---------------- UPDATE (optional image) ----------------

@router.put("/{id}", response_model=schemas.BlogWrappedResponse)

def update_blog(

    id:          int,

    blogTitle:   Annotated[Optional[str], Form()] = None,

    blogContent: Annotated[Optional[str], Form()] = None,

    description: Annotated[Optional[str], Form()] = None,

    image:       Annotated[Optional[UploadFile], File()] = None,

    db: Session = Depends(get_db)

):

    blog = repository.get_blog_by_id(db, id)

    if not blog:

        raise HTTPException(status_code=404, detail="Blog not found")



    update_data: dict = {}



    if blogTitle   is not None: update_data["blogTitle"]   = blogTitle

    if blogContent is not None: update_data["blogContent"] = blogContent

    if description is not None: update_data["description"] = description



    if image is not None and image.filename:

        # Delete old image from Azure/local before saving new one

        old_image_url = getattr(blog, "imageUrl", None)

        if old_image_url:

            delete_image(old_image_url)



        update_data["imageUrl"] = upload_image(image, container_key="blogs")



    updated_blog = repository.update_blog(db, id, update_data)



    return {

        "success": True,

        "message": "Blog updated successfully",

        "data": {**updated_blog.__dict__, "imageUrl": resolve_url(updated_blog.imageUrl)},

    }





# ---------------- GET BY ID ----------------

@router.get("/{id}", response_model=schemas.BlogResponse)

def get_blog_by_id(id: int, db: Session = Depends(get_db)):

    blog = repository.get_blog_by_id(db, id)

    if not blog:

        raise HTTPException(status_code=404, detail="Blog not found")

    return {**blog.__dict__, "imageUrl": resolve_url(blog.imageUrl)}





# ---------------- DELETE ----------------

@router.delete("/{id}", response_model=schemas.BlogWrappedResponse)

def delete_blog(id: int, db: Session = Depends(get_db)):

    # Fetch image URL BEFORE the record is deleted so we can clean up the blob

    existing = repository.get_blog_by_id(db, id)

    if not existing:

        raise HTTPException(status_code=404, detail="Blog not found")



    old_image_url = getattr(existing, "imageUrl", None)



    deleted_blog = repository.delete_blog(db, id)

    if not deleted_blog:

        raise HTTPException(status_code=404, detail="Blog not found")



    # Permanently remove the image from blob storage

    if old_image_url:

        delete_image(old_image_url)



    return {

        "success": True,

        "message": "Blog deleted permanently",

        "data": deleted_blog

    }