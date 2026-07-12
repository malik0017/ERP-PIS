from app.database.base import Base
from app.database.session import engine

# Import model modules so SQLAlchemy registers the tables.
import app.models  # noqa: F401


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("Production tables created/verified successfully.")


if __name__ == "__main__":
    main()