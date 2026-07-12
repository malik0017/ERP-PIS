# app/models/__init__.py

from app.models.company import Company
from app.models.user import User
from app.models.role import Role
from app.models.permission import Permission
from app.models.setting import SystemSetting
from app.models.audit_log import AuditLog

from app.models.ingredient import Ingredient
from app.models.recipe import Recipe, RecipeIngredient

from app.models.customer import Customer
from app.models.supplier import Supplier
from app.models.chef import Chef

from app.models.master_data import (
    MasterRecord,
    Brand,
    RevenueStream,
    KitchenLocation,
    KitchenSection,
)

from app.models.inventory import StockLot, InventoryTransaction

from app.models.production import (
    CustomerOrder,
    OrderLine,
    BOMLine,
    HeadChefPlan,
    StoreIssuanceLine,
    KitchenSectionTransaction,
    QCCheck,
    PackingDispatch,
)

# Optional/future modules
try:
    from app.models.notification import Notification
except Exception:
    pass

try:
    from app.models.finance import *
except Exception:
    pass

try:
    from app.models.warehouse import *
except Exception:
    pass