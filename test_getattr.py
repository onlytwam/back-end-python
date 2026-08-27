from pydantic import BaseModel
from typing import Optional

class StoresCreate(BaseModel):
    storeName: Optional[str] = None
    loginEmail: Optional[str] = None
    loginPassword: Optional[str] = None

data = StoresCreate(storeName="Test", loginEmail="test@test.com", loginPassword="pwd")
print(getattr(data, 'loginEmail', None))
