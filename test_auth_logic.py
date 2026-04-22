import sys
import os
import asyncio

# Ensure the parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import as part of the 'backend' package
from backend.auth.service import hash_password, verify_password, create_access_token

def test_password_hashing():
    password = "securepassword123"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrongpassword", hashed) is False
    print("✅ Password hashing test passed")

def test_jwt_generation():
    data = {"user_id": "123", "email": "test@example.com", "role": "student"}
    token = create_access_token(data)
    assert isinstance(token, str)
    assert len(token) > 0
    print("✅ JWT generation test passed")

async def main():
    print("Starting Auth Logic Tests...")
    test_password_hashing()
    test_jwt_generation()
    print("All logic tests passed!")

if __name__ == "__main__":
    asyncio.run(main())
