import sys
import uuid
from datetime import datetime, timezone
from sqlalchemy import text

sys.path.insert(0, r"C:\Users\Admin\.gemini\antigravity\scratch\TwamProject\Twam-live-BE")

from database import SessionLocal
from app.auth.security import _aspnet_hash_password, generate_sha256_hash_with_salt, _get_password_cipher

def create_user():
    db = SessionLocal()
    try:
        roles = db.execute(text('SELECT "UserRoleId", "RoleName" FROM twam."UserRole"')).fetchall()
        for r in roles:
            print(f"Role: {r.UserRoleId} - {r.RoleName}")
        
        inv_role = next((r for r in roles if "inventory" in (r.RoleName or "").lower()), None)
        
        if inv_role:
            print(f"Found Inventory Role: {inv_role.RoleName} (ID: {inv_role.UserRoleId})")
            
            email = "invmanager@onlytwam.com"
            db.execute(text(f"DELETE FROM auth.\"IdentityUser\" WHERE \"Email\" = '{email}'"))
            db.execute(text(f"DELETE FROM twam.\"People\" WHERE \"EmailId\" = '{email}'"))
            db.commit()

            user_id = str(uuid.uuid4())
            twam_hash = generate_sha256_hash_with_salt("Password123!", _get_password_cipher())
            pwd_hash = _aspnet_hash_password(twam_hash)
            now = datetime.now(timezone.utc).isoformat()
            
            db.execute(text(f"""
                INSERT INTO auth."IdentityUser" ("Id", "UserName", "NormalizedUserName", "Email", "NormalizedEmail", "EmailConfirmed", "PasswordHash", "UserRoleId", "IsActive", "CreatedDate", "LockoutEnabled")
                VALUES ('{user_id}', '{email}', '{email.upper()}', '{email}', '{email.upper()}', true, '{pwd_hash}', {inv_role.UserRoleId}, true, '{now}', false)
            """))
            
            db.execute(text(f"""
                INSERT INTO twam."People" ("EmailId", "FirstName", "LastName", "RoleId", "UserProfileId", "IsActive", "CreatedDate")
                VALUES ('{email}', 'Inventory', 'Manager', {inv_role.UserRoleId}, '{user_id}', true, '{now}')
            """))
            
            db.commit()
            print("Creation Result: Success")
        else:
            print("No inventory manager role found.")
    except Exception as e:
        print("Error:", e)
    finally:
        db.close()

if __name__ == "__main__":
    create_user()
