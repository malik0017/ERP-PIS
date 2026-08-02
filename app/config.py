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
# Batch 77: a hardcoded fallback here meant that a missing/misconfigured
# .env didn't stop the app — it started anyway, signing every session
# cookie with a secret that's sitting in plain text in the source code
# published on GitHub. Fail loud instead: a missing SECRET_KEY is a
# configuration error the person running the app needs to see immediately,
# not a state the app should ever run in silently.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<a long random string> to your "
        ".env file before starting the app — sessions cannot be signed safely "
        "without it. Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", 24))

# App
DEBUG = os.getenv("DEBUG", "False") == "True"
APP_NAME = os.getenv("APP_NAME", "ISFC PIMS")
COMPANY_NAME = os.getenv("COMPANY_NAME", "ISFC")