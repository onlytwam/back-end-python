import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import get_current_user
from . import repository, schemas


router = APIRouter(
    prefix="/api/ProductColor",
    tags=["ProductColor"],
    dependencies=[Depends(get_current_user)],
)


def parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


@router.get("", response_model=schemas.ProductColorListResponse)
def get_product_colors(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    return repository.get_all_product_colors(
        db,
        parse_filters(Filters),
        Order_Ascending,
        Order_Property,
        Page_Index,
        Page_Size,
    )


@router.get("/{product_color_id}", response_model=schemas.ProductColorResponse)
def get_product_color(product_color_id: int, db: Session = Depends(get_db)):
    result = repository.get_product_color_by_id(db, product_color_id)
    if not result:
        raise HTTPException(status_code=404, detail="ProductColor not found.")
    return result


@router.post("", status_code=201)
def create_product_color(command: schemas.ProductColorCreate, db: Session = Depends(get_db)):
    try:
        return {"productColorId": repository.create_product_color(db, command)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{product_color_id}")
def update_product_color(
    product_color_id: int,
    command: schemas.ProductColorUpdate,
    db: Session = Depends(get_db),
):
    try:
        result = repository.update_product_color(db, product_color_id, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result:
        raise HTTPException(status_code=404, detail="ProductColor not found.")
    return {"productColorId": result}


@router.delete("/{product_color_id}", status_code=204)
def delete_product_color(product_color_id: int, db: Session = Depends(get_db)):
    if not repository.delete_product_color(db, product_color_id):
        raise HTTPException(status_code=404, detail="ProductColor not found.")
