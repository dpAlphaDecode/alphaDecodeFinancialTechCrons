from pydantic import BaseModel, ConfigDict, EmailStr, constr
from typing import Optional

class UserCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: EmailStr
    password: constr(min_length=8)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class ForgotPassword(BaseModel):
    email: EmailStr

class GoogleLogin(BaseModel):
    token: str

class ResetPassword(BaseModel):
    email: EmailStr
    token: str
    new_password: constr(min_length=8)
