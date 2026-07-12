# app/core/localization.py

TRANSLATIONS = {
    "en": {
        "dashboard": "Dashboard",
        "production_orders": "Production Orders",
        "bakery_pastry": "Bakery/Pastry",
        "thawing": "Thawing",
        "cutting": "Cutting",
        "butchery": "Butchery",
        "marination": "Marination",
        "hot_kitchen": "Hot Kitchen",
        "cold_kitchen": "Cold Kitchen",
        "reports": "Reports",
        "settings": "Settings",
        "logout": "Logout",
    },
    "ar": {
        "dashboard": "لوحة التحكم",
        "production_orders": "أوامر الإنتاج",
        "bakery_pastry": "المخبوزات والحلويات",
        "thawing": "إذابة التجميد",
        "cutting": "التقطيع",
        "butchery": "الجزارة",
        "marination": "التتبيل",
        "hot_kitchen": "المطبخ الساخن",
        "cold_kitchen": "المطبخ البارد",
        "reports": "التقارير",
        "settings": "الإعدادات",
        "logout": "تسجيل الخروج",
    }
}


def t(key: str, lang: str = "en") -> str:
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, key)