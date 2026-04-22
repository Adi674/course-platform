from fastapi import APIRouter, Depends, status
from schemas import UserCreate, UserOut, LoginRequest, AccessTokenResponse
from auth import service

router = APIRouter(prefix="/auth", tags=["authentication"])

@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate):
    """
    Register a new user.
    """
    return await service.register_user(user_data)

@router.post("/login", response_model=AccessTokenResponse)
async def login(login_data: LoginRequest):
    """
    Login and receive an access token.
    """
    return await service.login_user(login_data)
