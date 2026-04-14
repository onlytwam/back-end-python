"""
app/auth/models.py
──────────────────
SQLAlchemy models for the auth schema.

Tables:
  auth.IdentityUser  — ASP.NET Core Identity user table (unchanged)
  auth.OtpStore      — short-lived OTPs supporting email and phone identifiers
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from database import Base
from app.userrole.models import UserRole


class IdentityUser(Base):
    __tablename__  = "IdentityUser"
    __table_args__ = {"schema": "auth"}

    Id                   = Column("Id",                   String,  primary_key=True, default=lambda: str(uuid.uuid4()))
    UserName             = Column("UserName",             String,  nullable=False)
    NormalizedUserName   = Column("NormalizedUserName",   String,  nullable=False, index=True)
    Email                = Column("Email",                String,  nullable=True)
    NormalizedEmail      = Column("NormalizedEmail",      String,  nullable=True,  index=True)
    EmailConfirmed       = Column("EmailConfirmed",       Boolean, default=False)
    PasswordHash         = Column("PasswordHash",         String,  nullable=True)
    SecurityStamp        = Column("SecurityStamp",        String,  nullable=True)
    ConcurrencyStamp     = Column("ConcurrencyStamp",     String,  nullable=True)
    PhoneNumber          = Column("PhoneNumber",          String,  nullable=True, index=True)
    PhoneNumberConfirmed = Column("PhoneNumberConfirmed", Boolean, default=False)
    TwoFactorEnabled     = Column("TwoFactorEnabled",     Boolean, default=False)
    LockoutEnd           = Column("LockoutEnd",           DateTime(timezone=True), nullable=True)
    LockoutEnabled       = Column("LockoutEnabled",       Boolean, default=True)
    AccessFailedCount    = Column("AccessFailedCount",    Integer, default=0)
    FirstName            = Column("FirstName",            String,  nullable=True)
    MiddleName           = Column("MiddleName",           String,  nullable=True)
    LastName             = Column("LastName",             String,  nullable=True)
    ProfileImageUrl      = Column("ProfileImageUrl",      String,  nullable=True)
    IsActive             = Column("IsActive",             Boolean, default=True)
    UserRoleId           = Column("UserRoleId",           Integer, nullable=True)
    CreatedDate          = Column("CreatedDate",          DateTime(timezone=True), default=func.now())
    CreatedBy            = Column("CreatedBy",            String,  nullable=True)
    ModifiedDate         = Column("ModifiedDate",         DateTime(timezone=True), nullable=True, onupdate=func.now())
    ModifiedBy           = Column("ModifiedBy",           String,  nullable=True)
    TenantId             = Column("TenantId",             String,  default="1")
    OrganizationId       = Column("OrganizationId",       Integer, nullable=True)
    DeletedInd           = Column("DeletedInd",           Boolean, default=False)
    IsRandomPassword     = Column("IsRandomPassword",     Boolean, default=False)

    def __repr__(self):
        return f"<IdentityUser(Id={self.Id}, UserName={self.UserName}, Email={self.Email})>"

    @property
    def full_name(self) -> str:
        parts = [self.FirstName or "", self.MiddleName or "", self.LastName or ""]
        return " ".join(p for p in parts if p).strip()

    def is_locked_out(self) -> bool:
        if not self.LockoutEnabled or self.LockoutEnd is None:
            return False
        return self.LockoutEnd > datetime.now(timezone.utc)

    def should_lock_account(self) -> bool:
        return self.AccessFailedCount >= 3


class OtpStore(Base):
    """
    Short-lived OTP records for all auth flows.

    Identifier     — email address OR 10-digit phone number
    IdentifierType — 'email' | 'phone'
    Purpose        — 'guest_checkout' | 'forgot_password' | 'login' | 'verify'
    UserId         — auth.IdentityUser.Id (set when user exists; used by
                     forgot_password to resolve the account via phone)
    ExpiresAt      — UTC expiry; cleaned up inline on every read
    Unique: one active OTP per (Identifier, Purpose) — re-sending refreshes row.
    """
    __tablename__ = "OtpStore"
    __table_args__ = (
        UniqueConstraint("Identifier", "Purpose", name="uq_otpstore_identifier_purpose"),
        {"schema": "auth"},
    )

    OtpId          = Column("OtpId",          BigInteger,              primary_key=True, autoincrement=True)
    Identifier     = Column("Identifier",     String(255),             nullable=False, index=True)
    IdentifierType = Column("IdentifierType", String(10),              nullable=False)
    Otp            = Column("Otp",            String(10),              nullable=False)
    Purpose        = Column("Purpose",        String(50),              nullable=False, default="guest_checkout")
    UserId         = Column("UserId",         String(255),             nullable=True)
    ExpiresAt      = Column("ExpiresAt",      DateTime(timezone=True), nullable=False)
    CreatedAt      = Column("CreatedAt",      DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return (f"<OtpStore(OtpId={self.OtpId}, Identifier={self.Identifier!r}, "
                f"Purpose={self.Purpose!r})>")