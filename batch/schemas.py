from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from uuid import UUID


class BatchCreate(BaseModel):
    name: str
    description: Optional[str] = None


class BatchUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class BatchOut(BaseModel):
    id: UUID
    teacher_id: UUID
    name: str
    description: Optional[str]
    batch_code: str          # Short alphanumeric code students use to self-enroll
    created_at: datetime

    class Config:
        from_attributes = True


class EnrollmentOut(BaseModel):
    id: UUID
    batch_id: UUID
    user_id: UUID
    enrolled_at: datetime
    enrolled_via: str        # "batch_code" | "payment" | "manual"

    class Config:
        from_attributes = True


class BatchDetailOut(BatchOut):
    """Batch with enrolled student count."""
    student_count: int


class JoinBatchRequest(BaseModel):
    batch_code: str          # Student submits this code to self-enroll