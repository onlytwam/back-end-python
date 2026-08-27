from database import SessionLocal
from app.stores.models import Stores
from app.billing.models import Inventory
from app.people.models import People
import traceback

db = SessionLocal()
try:
    entity = Stores(
        storeName="test_mapping",
        loginEmail="test@mapping.com",
        loginPassword="password123",
        deletedInd=False
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    print("Inserted store ID:", entity.storeId)
    
    from sqlalchemy import text
    res = db.execute(text('SELECT "StoreName", "LoginEmail", "LoginPassword" FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": entity.storeId}).fetchone()
    print("DB ROW:", res)
    
    db.execute(text('DELETE FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": entity.storeId})
    db.commit()
except Exception as e:
    traceback.print_exc()
finally:
    db.close()
