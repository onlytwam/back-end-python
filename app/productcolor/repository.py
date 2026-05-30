from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from app.shared.filters import apply_filters, build_paged_response
from .models import ProductColor


DEFAULT_PRODUCT_COLORS = [
    ("Black", "#111111"),
    ("White", "#f6f3ed"),
    ("Grey", "#808080"),
    ("Red", "#c0392b"),
    ("Pink", "#e75480"),
    ("Purple", "#7e57c2"),
    ("Blue", "#2f6fbd"),
    ("Green", "#2e7d32"),
    ("Yellow", "#f4c430"),
    ("Orange", "#e67e22"),
    ("Brown", "#795548"),
    ("Beige", "#d7c4a3"),
    ("Nude", "#d8b59b"),
    ("Gold", "#c8a64a"),
    ("Multi", "#8f8f8f"),
]


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _to_dict(row: ProductColor) -> dict:
    name = row.colorFamilyName or ""
    hex_code = row.hexCode or "#d0c8c2"
    return {
        "productColorId": row.productColorId,
        "colorFamilyId": row.productColorId,
        "colorFamilyName": name,
        "name": name,
        "hexCode": hex_code,
        "hex": hex_code,
        "description": row.description,
        "orderNo": row.orderNo,
        "state": row.state,
        "isActive": row.isActive if row.isActive is not None else True,
        "createdDate": row.createdDate,
        "modifiedDate": row.modifiedDate,
        "deletedInd": row.deletedInd if row.deletedInd is not None else False,
        "createdBy": row.createdBy,
        "modifiedBy": row.modifiedBy,
        "organizationId": row.organizationId,
    }


def get_all_product_colors(
    db: Session,
    filters=None,
    order_ascending: Optional[bool] = True,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
    active_only: bool = False,
) -> dict:
    query = db.query(ProductColor).filter(ProductColor.deletedInd == False)
    if active_only:
        query = query.filter(ProductColor.isActive == True)

    query = apply_filters(query, ProductColor, filters)
    if order_property:
        col = getattr(ProductColor, order_property, None)
        if col is not None:
            query = query.order_by(asc(col) if order_ascending else desc(col))
    else:
        query = query.order_by(
            ProductColor.orderNo.asc().nulls_last(),
            ProductColor.colorFamilyName.asc(),
        )

    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    return build_paged_response(total, [_to_dict(r) for r in query.all()])


def get_product_color_by_id(db: Session, product_color_id: int) -> Optional[dict]:
    row = db.query(ProductColor).filter(
        ProductColor.productColorId == product_color_id,
        ProductColor.deletedInd == False,
    ).first()
    return _to_dict(row) if row else None


def _duplicate_name_exists(db: Session, name: str, exclude_id: Optional[int] = None) -> bool:
    query = db.query(ProductColor).filter(
        ProductColor.deletedInd == False,
        func.lower(func.trim(ProductColor.colorFamilyName)) == name.lower().strip(),
    )
    if exclude_id:
        query = query.filter(ProductColor.productColorId != exclude_id)
    return db.query(query.exists()).scalar()


def create_product_color(db: Session, data) -> int:
    name = _clean(data.colorFamilyName)
    if not name:
        raise ValueError("Color family name is required.")
    if _duplicate_name_exists(db, name):
        raise ValueError("Color family already exists.")

    row = ProductColor(
        colorFamilyName=name,
        hexCode=_clean(data.hexCode) or "#d0c8c2",
        description=data.description,
        orderNo=data.orderNo,
        state=data.state or "Active",
        isActive=data.isActive,
        createdBy=getattr(data, "createdBy", None),
        organizationId=getattr(data, "organizationId", None),
        createdDate=datetime.now(timezone.utc),
        deletedInd=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.productColorId


def update_product_color(db: Session, product_color_id: int, data) -> Optional[int]:
    row = db.query(ProductColor).filter(
        ProductColor.productColorId == product_color_id,
        ProductColor.deletedInd == False,
    ).first()
    if not row:
        return None

    if data.colorFamilyName is not None:
        name = _clean(data.colorFamilyName)
        if not name:
            raise ValueError("Color family name is required.")
        if _duplicate_name_exists(db, name, product_color_id):
            raise ValueError("Color family already exists.")
        row.colorFamilyName = name
    if data.hexCode is not None:
        row.hexCode = _clean(data.hexCode) or "#d0c8c2"
    if "description" in data.model_fields_set:
        row.description = data.description
    if "orderNo" in data.model_fields_set:
        row.orderNo = data.orderNo
    if "state" in data.model_fields_set:
        row.state = data.state
    if data.isActive is not None:
        row.isActive = data.isActive
    if "organizationId" in data.model_fields_set:
        row.organizationId = data.organizationId
    row.modifiedBy = getattr(data, "modifiedBy", None) or row.modifiedBy
    row.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return row.productColorId


def delete_product_color(db: Session, product_color_id: int) -> bool:
    row = db.query(ProductColor).filter(
        ProductColor.productColorId == product_color_id,
        ProductColor.deletedInd == False,
    ).first()
    if not row:
        return False
    row.deletedInd = True
    row.isActive = False
    row.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True


def seed_default_product_colors(db: Session) -> None:
    for index, (name, hex_code) in enumerate(DEFAULT_PRODUCT_COLORS, start=1):
        exists = db.query(ProductColor).filter(
            ProductColor.deletedInd == False,
            func.lower(func.trim(ProductColor.colorFamilyName)) == name.lower(),
        ).first()
        if exists:
            continue
        db.add(ProductColor(
            colorFamilyName=name,
            hexCode=hex_code,
            orderNo=index,
            state="Active",
            isActive=True,
            createdDate=datetime.now(timezone.utc),
            deletedInd=False,
        ))
    db.commit()
