import bcrypt
import jwt
from datetime import datetime, timedelta
from typing import Optional
from config import settings
from database import get_supabase
from schemas import UserCreate, UserOut, LoginRequest, AccessTokenResponse
from exceptions import UnauthorizedError, ConflictError

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

async def register_user(user_data: UserCreate) -> UserOut:
    supabase = get_supabase()
    
    # Check if user exists
    existing_user = supabase.table("users").select("*").eq("email", user_data.email).execute()
    if existing_user.data:
        raise ConflictError("User with this email already exists")
    
    hashed_pwd = hash_password(user_data.password)
    
    # Insert user
    new_user = {
        "name": user_data.name,
        "email": user_data.email,
        "password": hashed_pwd,
        "role": user_data.role.value
    }
    
    result = supabase.table("users").insert(new_user).execute()
    if not result.data:
        raise Exception("Failed to create user")
        
    return UserOut(**result.data[0])

async def login_user(login_data: LoginRequest) -> AccessTokenResponse:
    supabase = get_supabase()
    
    # Find user
    result = supabase.table("users").select("*").eq("email", login_data.email).execute()
    if not result.data:
        raise UnauthorizedError("Invalid email or password")
    
    user = result.data[0]
    if not verify_password(login_data.password, user["password"]):
        raise UnauthorizedError("Invalid email or password")
    
    # Create token
    token_data = {
        "user_id": str(user["id"]),
        "email": user["email"],
        "role": user["role"]
    }
    
    access_token = create_access_token(token_data)
    
    return AccessTokenResponse(
        access_token=access_token,
        user=UserOut(**user)
    )
