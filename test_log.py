from database import SessionLocal
from app.stores.models import Stores
from app.billing.models import Inventory
from app.products.models import Products
from app.products.models import ProductVariantDetail
from app.people.models import People
import logging

logging.basicConfig()
logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

db = SessionLocal()
try:
    entity = Stores(
        storeName="test_mapping2",
        loginEmail="test@mapping2.com",
        loginPassword="password123",
        deletedInd=False
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    
    from sqlalchemy import text
    db.execute(text('DELETE FROM twam."Stores" WHERE "StoreId" = :sid'), {"sid": entity.storeId})
    db.commit()
finally:
    db.close()
