from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
import os

class Settings(BaseSettings):
    # Supabase - matching the user's .env keys
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str
    
    # Mapping for convenience if needed
    @property
    def SUPABASE_KEY(self) -> str:
        return self.SUPABASE_SERVICE_ROLE_KEY

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    
    # LiveKit
    LIVEKIT_URL: str
    LIVEKIT_API_KEY: str
    LIVEKIT_API_SECRET: str
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # CORS
    ALLOWED_ORIGINS: List[str] = ["*"]

    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "ap-south-1"                 
    AWS_S3_BUCKET_NAME: str = "edulive-recordings"
    AWS_S3_PRESIGNED_URL_EXPIRY: int = 3600        

    # Pydantic Settings Config
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        extra='allow'  # Allow extra fields to prevent validation errors if other vars exist
    )

settings = Settings()
