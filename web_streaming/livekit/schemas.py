from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from uuid import UUID


class RecordingOut(BaseModel):
    id: UUID
    classroom_id: UUID
    egress_id: str
    s3_key: Optional[str] = None          # S3 object key, set after egress completes
    url: Optional[str] = None             # Pre-signed URL, generated on demand
    status: str                           # "recording" | "completed" | "failed"
    started_at: datetime
    ended_at: Optional[datetime] = None

    class Config:
        from_attributes = True