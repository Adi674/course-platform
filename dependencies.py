import jwt
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import settings
from database import get_supabase
from schemas import UserOut, UserRole
from exceptions import UnauthorizedError, ForbiddenError

bearer_scheme = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> UserOut:
    """Decode Bearer JWT and return the authenticated user."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token")

    user_id = payload.get("user_id")
    if not user_id:
        raise UnauthorizedError("Invalid token payload")

    supabase = get_supabase()
    result = supabase.table("users").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise UnauthorizedError("User not found")

    return UserOut(**result.data)


async def require_teacher(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    """Require the authenticated user to be a teacher."""
    if current_user.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can perform this action")
    return current_user


async def require_student(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    """Require the authenticated user to be a student."""
    if current_user.role != UserRole.STUDENT:
        raise ForbiddenError("Only students can perform this action")
    return current_user


async def require_admin_or_teacher(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    """Require the authenticated user to be an admin or teacher."""
    if current_user.role not in (UserRole.ADMIN, UserRole.TEACHER):
        raise ForbiddenError("Only admins or teachers can perform this action")
    return current_user