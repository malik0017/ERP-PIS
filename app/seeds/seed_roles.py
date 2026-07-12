# app/seeds/seed_roles.py

from app.database.session import SessionLocal
from app.models.role import Role


ROLES = [
    "ADMIN",
    "GENERAL_MANAGER",
    "PRODUCTION_MANAGER",
    "PRODUCTION_SUPERVISOR",
    "PRODUCTION_OPERATOR",
    "STORE_MANAGER",
    "STORE_KEEPER",
    "PURCHASE_MANAGER",
    "FINANCE_MANAGER",
    "ACCOUNTANT",
    "HR_MANAGER",
    "SALES_MANAGER",
    "EMPLOYEE",
]


def role_exists(db, role_name: str):
    columns = Role.__table__.columns.keys()

    if "name" in columns:
        return db.query(Role).filter(Role.name == role_name).first()

    if "role_name" in columns:
        return db.query(Role).filter(Role.role_name == role_name).first()

    if "code" in columns:
        return db.query(Role).filter(Role.code == role_name).first()

    raise Exception("Role model must have name, role_name, or code column.")


def create_role(db, role_name: str):
    columns = Role.__table__.columns.keys()
    data = {}

    if "name" in columns:
        data["name"] = role_name

    if "role_name" in columns:
        data["role_name"] = role_name

    if "code" in columns:
        data["code"] = role_name

    if "description" in columns:
        data["description"] = role_name.replace("_", " ").title()

    if "is_active" in columns:
        data["is_active"] = True

    return Role(**data)


def main():
    db = SessionLocal()

    try:
        for role_name in ROLES:
            if not role_exists(db, role_name):
                db.add(create_role(db, role_name))

        db.commit()
        print("Roles inserted/verified successfully.")

    finally:
        db.close()


if __name__ == "__main__":
    main()