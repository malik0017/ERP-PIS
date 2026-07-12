# app/schemas/auth.py
from pydantic import BaseModel, EmailStr
from typing import Optional

# Request Models
class UserRegisterRequest(BaseModel):
    username: str
    email: EmailStr
    full_name: str
    password: str
    full_name_ar: Optional[str] = None

class UserLoginRequest(BaseModel):
    username: str
    password: str

# Response Models
class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    role: str
    language: str = "en"  # Added default value

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

class TokenData(BaseModel):
    user_id: int
    username: str
    role: str