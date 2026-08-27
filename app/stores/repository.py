from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from .models import Stores
from app.auth.repository import create_identity_user
from app.auth.schemas import RegisterUserRequest

def _enrich(db: Session, r: Stores) -> dict:
    country_name = None
    if r.country:
        try:
            row = db.execute(text('SELECT "CountryName" FROM mdm."Country" WHERE "CountryId"=:cid AND "DeletedInd"=false'),
                             {"cid": r.country}).fetchone()
            if row: country_name = row[0]
        except Exception: pass
    return {
        "storeId": r.storeId, "storeName": r.storeName, "country": r.country,
        "countryName": country_name, "city": r.city, "pinCode": r.pinCode,
        "address": r.address, "phone": r.phone, "email": r.email,
        "isPickUpAvailable": r.isPickUpAvailable, "mapLink": r.mapLink,
        "state": r.state, "isActive": r.isActive, "userProfileId": r.userProfileId,
        "createdDate": r.createdDate,
        "loginEmail": getattr(r, "loginEmail", None),
        "loginPassword": getattr(r, "loginPassword", None)
    }

def get_all(db: Session, filters, order_ascending, order_property, page_index, page_size):
    query = db.query(Stores).filter(Stores.deletedInd == False)
    if filters:
        for f in filters:
            col = getattr(Stores, f.get("property", ""), None)
            if col is not None: query = query.filter(col == f.get("value"))
    if order_property:
        col = getattr(Stores, order_property, None)
        if col: query = query.order_by(asc(col) if order_ascending is not False else desc(col))
    total = query.count()
    if page_index and page_size: query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"count": total, "list": [_enrich(db, r) for r in query.all()], "parameters": None}

def get_by_id(db: Session, store_id: int):
    r = db.query(Stores).filter(Stores.storeId == store_id, Stores.deletedInd == False).first()
    return _enrich(db, r) if r else None

def create(db: Session, data) -> int:
    entity = Stores(
        storeName=data.storeName, country=data.country, city=data.city,
        pinCode=data.pinCode, address=data.address, phone=data.phone,
        email=data.email, isPickUpAvailable=data.isPickUpAvailable,
        mapLink=data.mapLink, state=data.state, isActive=data.isActive,
        userProfileId=data.userProfileId, createdDate=datetime.now(timezone.utc), deletedInd=False, 
        loginEmail=getattr(data, 'loginEmail', None), loginPassword=getattr(data, 'loginPassword', None)
    )
    db.add(entity); db.commit(); db.refresh(entity)
    
    if getattr(data, 'loginEmail', None) and getattr(data, 'loginPassword', None):
        req = RegisterUserRequest(
            firstName=getattr(data, 'loginFirstName', 'Store'),
            lastName=getattr(data, 'loginLastName', 'Manager'),
            emailId=data.loginEmail,
            password=data.loginPassword,
            phoneNumber=getattr(data, 'loginPhone', ''),
            roleId=4,
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
    
    from app.people.models import People
    
    if getattr(data, 'loginEmail', None): entity.loginEmail = data.loginEmail
    if getattr(data, 'loginPassword', None): entity.loginPassword = data.loginPassword
    
    user = None
    if getattr(entity, 'loginEmail', None):
        user = db.query(People).filter(People.emailId == entity.loginEmail).first()
        
    identity = None
    if getattr(entity, 'loginEmail', None):
        from app.auth.models import IdentityUser
        identity = db.query(IdentityUser).filter(IdentityUser.Email == entity.loginEmail).first()

    if data.isActive is not None: 
        entity.isActive = data.isActive
        if user:
            user.isActive = data.isActive
        if identity:
            identity.IsActive = data.isActive
            
    if getattr(data, 'loginPassword', None) is not None and identity:
        from app.auth.security import _aspnet_hash_password, generate_sha256_hash_with_salt, _get_password_cipher
        import uuid
        twam_hash = generate_sha256_hash_with_salt(data.loginPassword, _get_password_cipher())
        identity.PasswordHash = _aspnet_hash_password(twam_hash)
        identity.SecurityStamp = str(uuid.uuid4())
    
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return entity.storeId

def delete(db: Session, store_id: int) -> bool:
    entity = db.query(Stores).filter(Stores.storeId == store_id).first()
    if not entity: return False
    
    from app.people.models import People
    if getattr(entity, 'loginEmail', None):
        user = db.query(People).filter(People.emailId == entity.loginEmail).first()
        if user:
            db.delete(user)
            
        from app.auth.models import IdentityUser
        identity = db.query(IdentityUser).filter(IdentityUser.Email == entity.loginEmail).first()
        if identity:
            db.delete(identity)
            
    db.delete(entity)
    db.commit()
    return True

