@router.get(
    "/inventory",
    response_model=List[InventoryItem],
    summary="Return inventory for the logged-in user's store",
)
def list_inventory(
    db:           Session     = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    from sqlalchemy import text
    sql = """
        SELECT 
            i."InventoryId", i."StockId", i."StoreId", i."WarehouseId",
            i."Quantity", i."available",
            p."ProductName", p."ProductCode", c."ColorName", s."SizeName"
        FROM billing."Inventory" i
        LEFT JOIN product."ProductVariantDetail" pvd ON pvd."StockId" = i."StockId"
        LEFT JOIN product."ProductVariant" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN product."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN mdm."ProductColor" c ON c."ColorId" = pv."ColorId"
        LEFT JOIN mdm."Size" s ON s."SizeId" = pvd."SizeId"
        WHERE 1=1
    """
    params = {}
    
    if current_user.role_id in (SUPER_ADMIN_ROLE_ID, 3) and current_user.store_id is None and current_user.warehouse_id is None:
        pass
    elif current_user.store_id:
        sql += ' AND i."StoreId" = :sid'
        params['sid'] = current_user.store_id
    elif current_user.warehouse_id:
        sql += ' AND i."WarehouseId" = :wid'
        params['wid'] = current_user.warehouse_id
        
    sql += ' ORDER BY i."StockId"'
    rows = db.execute(text(sql), params).fetchall()
    
    # Resolve names
    stores = {r[0]: r[1] for r in db.execute(text('SELECT "StoreId", "StoreName" FROM twam."Stores"')).fetchall()}
    whs = {r[0]: r[1] for r in db.execute(text('SELECT "WarehouseId", "WarehouseName" FROM billing."Warehouse"')).fetchall()}
    
    result = []
    for row in rows:
        loc_type, loc_name = "Unknown", "Unknown"
        if row[2]:
            loc_type = "Store"
            loc_name = stores.get(row[2], f"Store {row[2]}")
        elif row[3]:
            loc_type = "Warehouse"
            loc_name = whs.get(row[3], f"Warehouse {row[3]}")
            
        result.append({
            "inventoryId": row[0],
            "stockId": row[1],
            "storeId": row[2],
            "warehouseId": row[3],
            "locationName": loc_name,
            "locationType": loc_type,
            "quantity": row[4],
            "available": row[5],
            "productName": row[6],
            "productCode": row[7],
            "colorName": row[8],
            "sizeName": row[9]
        })
    return result
