from pydantic import BaseModel, EmailStr
from enum import Enum
from typing import Optional, List
from datetime import datetime
from uuid import UUID

class UserRole(str, Enum):
    TEACHER = "teacher"
    STUDENT = "student"
    SUPPORT = "support"
    ADMIN = "admin"

class ClassroomStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    ENDED = "ended"

class UserBase(BaseModel):
    name: str
    email: EmailStr
    role: UserRole = UserRole.STUDENT

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: UUID
    created_at: datetime

    class Config:
        from_attributes = True

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

class TokenData(BaseModel):
    user_id: str
    email: str
    role: str
