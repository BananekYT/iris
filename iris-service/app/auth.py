import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .errors import JwtSecretMissingError

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30
security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


def _get_secret_key() -> str:
    env = os.getenv("ENV", "development").lower()
    secret = os.getenv("JWT_SECRET")

    if env == "production" and not secret:
        raise JwtSecretMissingError(
            "Brak JWT_SECRET dla środowiska production. Ustaw zmienną środowiskową."
        )

    return secret or "CHANGE_ME_IN_DEV"


def create_access_token(user_id: str):
    payload = {
        "sub": user_id,
        "exp": datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    try:
        token = credentials.credentials
        payload = jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])
        return payload["sub"]
    except JwtSecretMissingError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token") from exc


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> str | None:
    if credentials is None:
        return None
    return get_current_user(credentials)
