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

# --- Classroom schemas ---

class ClassroomCreate(BaseModel):
    title: str
    batch_id: UUID
    description: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    duration_minutes: int = 60

class ClassroomOut(BaseModel):
    id: UUID
    teacher_id: UUID
    batch_id: UUID
    title: str
    description: Optional[str] = None
    room_name: str
    join_token: str
    status: ClassroomStatus
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: datetime
    duration_minutes: int

    class Config:
        from_attributes = True

# --- LiveKit / Streaming schemas ---

class LiveKitTokenResponse(BaseModel):
    token: str
    room_name: str
    classroom_id: str
    classroom_title: str
    can_publish: bool

class ParticipantOut(BaseModel):
    id: UUID
    name: str
    email: EmailStr
    role: UserRole

class MicActionRequest(BaseModel):
    student_id: UUID

class MicGrantRequest(BaseModel):
    """
    Optional request body for mic grant — reserved for future metadata
    (e.g. duration_seconds to auto-revoke after N seconds).
    Currently empty; student_id is passed as a path parameter.
    """
    pass
 
 
class StudentMicStatusOut(BaseModel):
    """
    Returned by GET /classrooms/{id}/mic/status so the teacher can see
    which students currently have individual mic grants.
    """
    student_id: UUID
    name: str
    email: str
    mic_granted: bool
 
 
class TokenRefreshResponse(BaseModel):
    """
    Returned by GET /classrooms/{id}/token/refresh.
    Contains a freshly-signed LiveKit JWT reflecting the student's current
    mic permissions without requiring them to re-join the room.
    """
    token: str
    can_publish: bool
    can_publish_audio: bool