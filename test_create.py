from database import SessionLocal
from app.stores.schemas import StoresCreate
from app.stores.repository import create
from app.billing.models import Inventory

db = SessionLocal()
try:
    data = StoresCreate(storeName="test_store2", loginEmail="test2@test.com", loginPassword="testpassword2")
    print("Schema parsed:", data.dict())
    
    # Actually call create
    store_id = create(db, data)
    print("Created Store ID:", store_id)
    
    from sqlalchemy import text
    res = db.execute(text('SELECT "StoreId", "StoreName", "LoginEmail", "LoginPassword" FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": store_id}).fetchall()
    print("DB ROW:", res)
finally:
    db.close()
