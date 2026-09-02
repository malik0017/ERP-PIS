from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

from app.models.customer import Customer  
from app.models.supplier import Supplier  
from app.models.chef import Chef  
from app.models.ingredient import Ingredient  
from app.models.recipe import Recipe, RecipeIngredient  
from app.models.master_data import Brand, RevenueStream, KitchenLocation, KitchenSection, MasterRecord  
