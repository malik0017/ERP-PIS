# app/core/config.py
from dotenv import load_dotenv
import os
from pydantic_settings import BaseSettings


load_dotenv()


class Settings:

    DATABASE_URL = os.getenv("DATABASE_URL")

    SECRET_KEY = os.getenv("SECRET_KEY")

    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")

    JWT_EXPIRE_HOURS = int(
        os.getenv("JWT_EXPIRE_HOURS", 24)
    )

    APP_NAME = os.getenv("APP_NAME")


settings = Settings() 