# app/seeds/seed_admin_user.py

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


def main():
    db = SessionLocal()

    try:
        admin_role = get_or_create_admin_role(db)
        company = get_or_create_company(db)

        user = db.query(User).filter(
            User.username == "admin"
        ).first()

        if user:
            user.password_hash = hash_password("admin123")
            user.role_id = admin_role.id
            user.company_id = company.id
            user.is_active = True
            user.is_verified = True
            db.commit()

            print("Admin user already existed.")
            print("Password reset successfully.")
            print("Username: admin")
            print("Password: admin123")
            return

        user = User(
            username="admin",
            email="admin@isfc.local",
            full_name="System Administrator",
            full_name_ar="مدير النظام",
            password_hash=hash_password("admin123"),
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

        print("Admin user created successfully.")
        print("Username: admin")
        print("Password: admin123")

    finally:
        db.close()


if __name__ == "__main__":
    main()