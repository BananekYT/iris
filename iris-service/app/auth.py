import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .errors import JwtSecretMissingError

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "12"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
security = HTTPBearer()

_token_lock = threading.Lock()
_revoked_access_jti: set[str] = set()
_refresh_tokens: dict[str, dict] = {}
_user_refresh_index: dict[str, set[str]] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_secret_key() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise JwtSecretMissingError("Brak JWT_SECRET. Ustaw zmienną środowiskową.")
    return secret


def _encode_token(payload: dict) -> str:
    return jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])


def _build_claims(
    user_id: str,
    role: str,
    token_type: str,
    expires_delta: timedelta,
    family_id: str | None = None,
) -> dict:
    now = _utc_now()
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "role": role,
        "typ": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if family_id:
        payload["family_id"] = family_id
    return payload


def _register_refresh_token(payload: dict) -> None:
    jti = payload["jti"]
    user_id = payload["sub"]
    with _token_lock:
        _refresh_tokens[jti] = {
            "user_id": user_id,
            "exp": payload["exp"],
            "revoked": False,
            "family_id": payload.get("family_id"),
            "rotated_to": None,
        }
        _user_refresh_index.setdefault(user_id, set()).add(jti)


def create_access_token(user_id: str, role: str = "Uczen") -> str:
    payload = _build_claims(
        user_id=user_id,
        role=role,
        token_type="access",
        expires_delta=timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    )
    return _encode_token(payload)


def create_token_pair(user_id: str, role: str = "Uczen") -> dict:
    family_id = str(uuid.uuid4())
    access_payload = _build_claims(
        user_id=user_id,
        role=role,
        token_type="access",
        expires_delta=timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
        family_id=family_id,
    )
    refresh_payload = _build_claims(
        user_id=user_id,
        role=role,
        token_type="refresh",
        expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        family_id=family_id,
    )
    access_token = _encode_token(access_payload)
    refresh_token = _encode_token(refresh_payload)
    _register_refresh_token(refresh_payload)
    return {
        "token_type": "bearer",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_expires_in": ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        "refresh_expires_in": REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
    }


def refresh_token_pair(refresh_token: str) -> dict:
    try:
        payload = _decode_token(refresh_token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token odświeżający") from exc
    if payload.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Nieprawidłowy token odświeżający")

    jti = payload.get("jti")
    user_id = payload.get("sub")
    role = payload.get("role", "Uczen")
    family_id = payload.get("family_id")
    now_ts = int(_utc_now().timestamp())

    with _token_lock:
        stored = _refresh_tokens.get(jti)
        if not stored or stored.get("revoked"):
            raise HTTPException(status_code=401, detail="Token odświeżający został unieważniony")
        if stored["exp"] < now_ts:
            stored["revoked"] = True
            raise HTTPException(status_code=401, detail="Token odświeżający wygasł")

        # Rotacja refresh tokenu.
        stored["revoked"] = True

    new_pair = create_token_pair(user_id=user_id, role=role)
    new_refresh_payload = _decode_token(new_pair["refresh_token"])
    with _token_lock:
        stored = _refresh_tokens.get(jti)
        if stored:
            stored["rotated_to"] = new_refresh_payload["jti"]
            if family_id:
                _refresh_tokens[new_refresh_payload["jti"]]["family_id"] = family_id
    return new_pair


def revoke_access_token(access_jti: str) -> None:
    with _token_lock:
        _revoked_access_jti.add(access_jti)


def revoke_refresh_token(refresh_token: str) -> None:
    try:
        payload = _decode_token(refresh_token)
    except JWTError:
        return
    if payload.get("typ") != "refresh":
        return
    jti = payload.get("jti")
    with _token_lock:
        stored = _refresh_tokens.get(jti)
        if stored:
            stored["revoked"] = True


def revoke_all_refresh_tokens_for_user(user_id: str) -> None:
    with _token_lock:
        for jti in _user_refresh_index.get(user_id, set()):
            stored = _refresh_tokens.get(jti)
            if stored:
                stored["revoked"] = True


def _validate_access_payload(payload: dict) -> dict:
    if payload.get("typ") != "access":
        raise HTTPException(status_code=401, detail="Nieprawidłowy typ tokenu")
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token")
    with _token_lock:
        if jti in _revoked_access_jti:
            raise HTTPException(status_code=401, detail="Token został unieważniony")
    return payload


def get_current_claims(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    try:
        token = credentials.credentials
        payload = _decode_token(token)
        return _validate_access_payload(payload)
    except JwtSecretMissingError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token") from exc


def get_current_user(
    claims: dict = Depends(get_current_claims),
) -> str:
    return str(claims["sub"])


def get_current_role(
    claims: dict = Depends(get_current_claims),
) -> str:
    return str(claims.get("role", "Uczen"))


def try_get_subject_from_auth_header(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = _decode_token(token)
        return str(payload.get("sub")) if payload.get("sub") else None
    except Exception:
        return None
