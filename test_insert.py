import os
os.environ["DATABASE_URL"] = "postgresql://rndtechies:FpyZgFZ08FXnzNG088MgUsZpAeTSmdOkDcZJZkTlTAwdbYXtNNjcJQQJ99CHACAAAAAAAAAAAAASAZDO2Smx@twampostgres.postgres.database.azure.com:5432/postgres?sslmode=require"

from database import SessionLocal
from main import app  # This imports all routers and models properly

from app.stores.models import Stores

db = SessionLocal()
try:
    entity = Stores(
        storeName="test_insert_3",
        loginEmail="test@test3.com",
        loginPassword="password123",
        deletedInd=False
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    print("Inserted store ID:", entity.storeId)
    print("Python object loginEmail:", entity.loginEmail)
    
    from sqlalchemy import text
    res = db.execute(text('SELECT "StoreName", "LoginEmail", "LoginPassword" FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": entity.storeId}).fetchone()
    print("DB ROW:", res)
    
    db.execute(text('DELETE FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": entity.storeId})
    db.commit()
finally:
    db.close()
