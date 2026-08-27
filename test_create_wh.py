import os
os.environ["DATABASE_URL"] = "postgresql://rndtechies:FpyZgFZ08FXnzNG088MgUsZpAeTSmdOkDcZJZkTlTAwdbYXtNNjcJQQJ99CHACAAAAAAAAAAAAASAZDO2Smx@twampostgres.postgres.database.azure.com:5432/postgres?sslmode=require"

from database import SessionLocal
from main import app
from app.billing.schemas import WarehouseCreate
from app.billing.router import create_warehouse
from app.auth.dependencies import CurrentUser

db = SessionLocal()
try:
    payload = WarehouseCreate(
        warehouseName="Test Warehouse 100",
        address="Test Address",
        loginEmail="test@warehouse100.com",
        loginPassword="password123"
    )
    current_user = CurrentUser(user_id="test", email="test@test.com", role_id=3, role_name="Inventory Manager", first_name="test", last_name="test", is_active=True, profile_image_url="") # Inventory manager
    
    result = create_warehouse(payload=payload, db=db, current_user=current_user)
    print("Warehouse created successfully:", result)
    
    # Clean up
    from sqlalchemy import text
    db.execute(text('DELETE FROM twam."Warehouses" WHERE "WarehouseId" = :wid'), {"wid": result.warehouseId})
    db.execute(text('DELETE FROM twam."People" WHERE "EmailId" = :email'), {"email": "test@warehouse100.com"})
    db.execute(text('DELETE FROM twam."IdentityUser" WHERE "Email" = :email'), {"email": "test@warehouse100.com"})
    db.commit()
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    db.close()
