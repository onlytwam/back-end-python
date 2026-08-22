from sqlalchemy.orm import Session
from .models import Stores
from datetime import datetime, timezone
from typing import Optional
from app.auth.schemas import RegisterUserRequest

def create(db: Session, data) -> int:
    entity = Stores(
        storeName=data.storeName, country=data.country, city=data.city,
        pinCode=data.pinCode, address=data.address, phone=data.phone,
        email=data.email, isPickUpAvailable=data.isPickUpAvailable,
        mapLink=data.mapLink, state=data.state, isActive=data.isActive,
        userProfileId=data.userProfileId, createdDate=datetime.now(timezone.utc), deletedInd=False, loginEmail=getattr(data, 'loginEmail', None), loginPassword=getattr(data, 'loginPassword', None)
    )
    db.add(entity); db.commit(); db.refresh(entity)
    
    if getattr(data, 'loginEmail', None) and getattr(data, 'loginPassword', None):
        from app.auth.repository import create_identity_user
        req = RegisterUserRequest(
            firstName=getattr(data, 'loginFirstName', 'Store'),
            lastName=getattr(data, 'loginLastName', 'Manager'),
            emailId=data.loginEmail,
            password=data.loginPassword,
            phoneNumber=getattr(data, 'loginPhone', ''),
            roleId=4, # Store/Warehouse Manager
            storeId=entity.storeId
        )
        create_identity_user(db, req)
    
    return entity.storeId

def update(db: Session, data) -> Optional[int]:
    entity = db.query(Stores).filter(Stores.storeId == data.storeId).first()
    if not entity: return None
    
    if data.storeName is not None: entity.storeName = data.storeName
    if data.country is not None: entity.country = data.country
    if data.city is not None: entity.city = data.city
    if data.pinCode is not None: entity.pinCode = data.pinCode
    if data.address is not None: entity.address = data.address
    if data.phone is not None: entity.phone = data.phone
    if data.email is not None: entity.email = data.email
    if data.isPickUpAvailable is not None: entity.isPickUpAvailable = data.isPickUpAvailable
    if data.mapLink is not None: entity.mapLink = data.mapLink
    if data.state is not None: entity.state = data.state
    if data.isActive is not None: entity.isActive = data.isActive
    if getattr(data, 'loginEmail', None): entity.loginEmail = data.loginEmail
    if getattr(data, 'loginPassword', None): entity.loginPassword = data.loginPassword
    
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return entity.storeId

def delete(db: Session, store_id: int) -> bool:
    entity = db.query(Stores).filter(Stores.storeId == store_id).first()
    if not entity: return False
    entity.deletedInd = True; entity.modifiedDate = datetime.now(timezone.utc)
    db.commit(); return True

def list_all(db: Session):
    return db.query(Stores).filter(Stores.deletedInd == False).all()
