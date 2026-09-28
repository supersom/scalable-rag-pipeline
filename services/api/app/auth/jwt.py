# services/api/app/auth/jwt.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from services.api.app.config import settings
import time
import re

tenant_pattern = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')

# OAuth2 scheme tells Swagger UI where to send the token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Validates the JWT Token from the Authorization header.
    Decodes user info (ID, Role, Permissions).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # 1. Decode Token
        # Verify signature using the Secret Key defined in Config
        payload = jwt.decode(
            token, 
            settings.JWT_SECRET_KEY, 
            algorithms=[settings.JWT_ALGORITHM]
        )
        
        user_id: str = payload.get("sub")
        role: str = payload.get("role", "user")
        
        if user_id is None:
            raise credentials_exception
            
        # 2. Check Expiration (Redundant if jwt.decode does it, but good for safety)
        exp = payload.get("exp")
        if exp and time.time() > exp:
            raise HTTPException(status_code=401, detail="Token expired")

        tenant = payload.get('tenant_id')
        tenant = tenant if isinstance(tenant, str) and tenant_pattern.fullmatch(tenant) else None
        # Only the verified optional claim authorizes a capture tenant.
        return {
            "id": user_id, 
            "role": role,
            "permissions": payload.get("permissions", []),
            "tenant_id": tenant,
        }
        
    except JWTError:
        raise credentials_exception
