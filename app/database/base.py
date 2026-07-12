from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

# Import models so metadata knows about them when init/create_all is used.
from app.models.customer import Customer  # noqa: E402,F401
from app.models.supplier import Supplier  # noqa: E402,F401
from app.models.chef import Chef  # noqa: E402,F401
from app.models.ingredient import Ingredient  # noqa: E402,F401
from app.models.recipe import Recipe, RecipeIngredient  # noqa: E402,F401
from app.models.master_data import Brand, RevenueStream, KitchenLocation, KitchenSection, MasterRecord  # noqa: E402,F401
