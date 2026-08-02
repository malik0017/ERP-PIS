# app/seeds/seed_admin_user.py
#
# Batch 77 fix: this used to hash the literal string "admin123" as the
# admin password EVERY time it ran — including against an already-existing
# admin account, silently overwriting any password that had since been
# changed. That's a double problem: a well-known default credential, and a
# script that quietly resets it back even after someone fixes it.
#
# New behavior:
#   - First run (no admin user exists yet): creates one with either
#     ADMIN_DEFAULT_PASSWORD from the environment, or a freshly generated
#     random password printed ONCE to the console. force_password_change
#     is set so the first real login should change it.
#   - Subsequent runs (admin user already exists): NEVER touches the
#     password. Only backfills role/company/active/verified if somehow
#     missing. This makes the script safe to re-run as part of any future
#     setup automation without undoing a real password change.

import os
import secrets

from app.database.session import SessionLocal
from app.models.user import User
from app.models.role import Role
from app.models.company import Company
from app.core.security import hash_password


def get_role_name_column():
    columns = Role.__table__.columns.keys()

    if "name" in columns:
        return "name"

    if "role_name" in columns:
        return "role_name"

    if "code" in columns:
        return "code"

    raise Exception("Role model must have name, role_name, or code column.")


def get_or_create_admin_role(db):
    role_column = get_role_name_column()

    role = db.query(Role).filter(
        getattr(Role, role_column) == "ADMIN"
    ).first()

    if role:
        return role

    data = {
        role_column: "ADMIN"
    }

    if "description" in Role.__table__.columns.keys():
        data["description"] = "System Administrator"

    if "is_active" in Role.__table__.columns.keys():
        data["is_active"] = True

    role = Role(**data)
    db.add(role)
    db.commit()
    db.refresh(role)

    return role


def get_or_create_company(db):
    company = db.query(Company).first()

    if company:
        return company

    company = Company(
        name="International Specialized Food Company",
        name_ar="الشركة العالمية المتخصصة للأغذية",
        email="info@isfc.local",
        phone="0000000000",
        address="Riyadh",
        logo=None
    )

    db.add(company)
    db.commit()
    db.refresh(company)

    return company


def _try_set_force_password_change(db, user_id: int) -> None:
    """Best-effort: set force_password_change if that column exists on this
    DB copy. Uses its own try/except rather than the auth module's schema
    helper so this seed script has no import-time dependency on the auth
    router (keeps it runnable standalone, as it always has been)."""
    from sqlalchemy import text
    try:
        db.execute(text("UPDATE users SET force_password_change = 1 WHERE id = :id"), {"id": user_id})
        db.commit()
    except Exception:
        db.rollback()


def main():
    db = SessionLocal()

    try:
        admin_role = get_or_create_admin_role(db)
        company = get_or_create_company(db)

        user = db.query(User).filter(
            User.username == "admin"
        ).first()

        if user:
            # Existing account: backfill role/company/active state if it's
            # somehow missing. The password is never touched here again.
            changed = False
            if user.role_id != admin_role.id:
                user.role_id = admin_role.id
                changed = True
            if not user.company_id:
                user.company_id = company.id
                changed = True
            if not user.is_active:
                user.is_active = True
                changed = True
            if changed:
                db.commit()
                print("Admin user already existed — role/company/active state backfilled.")
            else:
                print("Admin user already existed — nothing to change.")
            print("Password was NOT modified. Use the Users & Access screen "
                  "(or its 'Reset password' action) if you need to change it.")
            return

        # First run: create the admin account with a real, non-guessable password.
        env_password = os.getenv("ADMIN_DEFAULT_PASSWORD")
        password = env_password or secrets.token_urlsafe(12)

        user = User(
            username="admin",
            email="admin@isfc.local",
            full_name="System Administrator",
            full_name_ar="مدير النظام",
            password_hash=hash_password(password),
            employee_code="ADMIN-001",
            phone="0000000000",
            avatar=None,
            company_id=company.id,
            role_id=admin_role.id,
            is_active=True,
            is_verified=True,
            preferred_language="en",
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        _try_set_force_password_change(db, user.id)

        print("=" * 60)
        print("Admin user created successfully.")
        print("Username: admin")
        if env_password:
            print("Password: set from ADMIN_DEFAULT_PASSWORD in the environment.")
        else:
            print(f"Password (generated, shown once): {password}")
            print("Save this now - it is not stored anywhere in plain text")
            print("and will not be shown again. Change it after first login.")
        print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
