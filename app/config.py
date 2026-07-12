# app/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# Security
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-before-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", 24))

# App
DEBUG = os.getenv("DEBUG", "False") == "True"
APP_NAME = os.getenv("APP_NAME", "ISFC PIMS")
COMPANY_NAME = os.getenv("COMPANY_NAME", "ISFC")