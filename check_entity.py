from database import SessionLocal
from app.billing.models import Inventory
from app.stores.models import Stores
db = SessionLocal()
store = db.query(Stores).first()
if store:
    print(dir(store))
    print("LoginEmail:", getattr(store, 'loginEmail', 'MISSING'))
