import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from . import schemas, repository
from app.auth.dependencies import get_current_user
from app.sendemail import repository as email_repository
from app.sendemail.schemas import EmailCreate

router = APIRouter(
    prefix="/api/SupportRequest",
    tags=["SupportRequest"],
    dependencies=[Depends(get_current_user)],
)


# ─────────────────────────────────────────────
# Get List
# ─────────────────────────────────────────────
@router.get("/", response_model=schemas.SupportRequestListResponse)
def get_list(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    try:
        parsed_filters = json.loads(Filters) if Filters else None
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format")

    return repository.get_all(
        db,
        parsed_filters,
        Order_Ascending,
        Order_Property,
        Page_Index,
        Page_Size,
    )


# ─────────────────────────────────────────────
# Get Detail
# ─────────────────────────────────────────────
@router.get("/{supportId}", response_model=schemas.SupportRequestResponse)
def get_detail(supportId: int, db: Session = Depends(get_db)):
    result = repository.get_by_id(db, supportId)
    if not result:
        raise HTTPException(status_code=404, detail="SupportRequest not found")
    return result


# ─────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────
@router.post("/", status_code=201)
def create(command: schemas.SupportRequestCreate, db: Session = Depends(get_db)):
    support_id = repository.create(db, command)
    return {"supportId": support_id}


# ─────────────────────────────────────────────
# Update + Send Reply Email
# ─────────────────────────────────────────────
@router.put("/{supportId}")
def update(
    supportId: int,
    command: schemas.SupportRequestUpdate,
    db: Session = Depends(get_db),
):
    print(f"DEBUG → supportId={supportId}, remarks='{command.remarks}', state='{command.state}'")
    
    command.supportId = supportId
    result = repository.update(db, command)
    if not result:
        raise HTTPException(status_code=404, detail="SupportRequest not found")

    if command.remarks:
        entity = repository.get_by_id(db, supportId)
        print(f"DEBUG → entity email='{entity.get('email') if entity else None}'")
        if entity and entity.get("email"):
            try:
                email_payload = EmailCreate(
                    name=entity.get("name") or "Customer",
                    email=entity["email"],
                    subject=f"Re: {entity.get('subject') or 'Your Support Request'}",
                    message=_build_email_html(
                        name=entity.get("name") or "Customer",
                        subject=entity.get("subject") or "Your Support Request",
                        reply=command.remarks,
                        state=command.state or "Open",
                    ),
                )
                result_email = email_repository.send_email(db, email_payload)
                print(f"DEBUG → email send result: {result_email}")
            except Exception as e:
                print(f"❌ Support reply email failed: {e}")
    else:
        print("DEBUG → remarks is empty, skipping email")

    return {"supportId": result}
# ─────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────
@router.delete("/{supportId}", status_code=204)
def delete(supportId: int, db: Session = Depends(get_db)):
    if not repository.delete(db, supportId):
        raise HTTPException(status_code=404, detail="SupportRequest not found")


# ─────────────────────────────────────────────
# Email HTML Template
# ─────────────────────────────────────────────
def _build_email_html(name: str, subject: str, reply: str, state: str) -> str:
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 24px; border: 1px solid #eee; border-radius: 8px;">
        <h2 style="color: #333; margin-bottom: 4px;">Support Request Update</h2>
        <p style="color: #888; font-size: 13px; margin-top: 0;">Status: <strong>{state}</strong></p>
        <hr style="border: none; border-top: 1px solid #eee;" />
        <p>Hi <strong>{name}</strong>,</p>
        <p>Our support team has responded to your request:</p>
        <p style="color: #555;"><strong>{subject}</strong></p>
        <div style="background: #f4f4f4; padding: 16px; border-left: 4px solid #4f46e5; margin: 16px 0; border-radius: 4px;">
            <p style="margin: 0; color: #333; white-space: pre-line;">{reply}</p>
        </div>
        <p>If you have further questions, feel free to contact us again.</p>
        <p style="color: #aaa; font-size: 12px; margin-top: 32px;">— Support Team</p>
    </div>
    """


@router.post("/test-email")
def test_email(db: Session = Depends(get_db)):
    """Temporary debug endpoint — remove after testing"""
    
    # Step 1: Check active SMTP credential exists
    from app.sendemail.models import EmailCredential
    cred = db.query(EmailCredential).filter(
        EmailCredential.isActive == True,
        EmailCredential.deletedInd == False
    ).first()
    
    if not cred:
        return {"error": "❌ No active SMTP credential found in DB"}
    
    print(f"✅ Found credential: {cred.smtpHost}:{cred.smtpPort} user={cred.smtpUser}")

    # Step 2: Try sending a test email
    try:
        email_payload = EmailCreate(
            name="Test User",
            email="REPLACE_WITH_YOUR_EMAIL@gmail.com",  # ← put your own email here
            subject="Test Support Reply",
            message="<p>This is a test email from the support system.</p>",
        )
        result = email_repository.send_email(db, email_payload)
        return {
            "credentialFound": True,
            "smtpHost": cred.smtpHost,
            "smtpPort": cred.smtpPort,
            "smtpUser": cred.smtpUser,
            "emailSent": result,
        }
    except Exception as e:
        return {"error": str(e)}