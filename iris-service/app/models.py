from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    pin: str = Field(..., min_length=1, examples=["1234"])
    token: str = Field(..., min_length=1, examples=["ABCDEF"])
    tenant: str = Field(..., min_length=1, examples=["warszawa"])
    device_name: str = Field(default="Android", examples=["Android"])
    device_model: str = Field(default="SM-A525F", examples=["SM-A525F"])


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10, examples=["eyJhbGciOiJIUzI1NiIs..."])


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, examples=["eyJhbGciOiJIUzI1NiIs..."])
    all_sessions: bool = Field(default=False, examples=[False])


class SelectAccountRequest(BaseModel):
    pupil_id: int = Field(..., examples=[123456])


class TokenPairResponse(BaseModel):
    token_type: Literal["bearer"] = "bearer"
    access_token: str
    refresh_token: str
    access_expires_in: int = Field(..., description="Czas życia access tokenu w sekundach.")
    refresh_expires_in: int = Field(..., description="Czas życia refresh tokenu w sekundach.")


class ApiErrorBody(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorBody


class PaginationMeta(BaseModel):
    total: int
    offset: int
    limit: int


class PaginatedResponse(BaseModel):
    items: list[dict]
    pagination: PaginationMeta


class DeltaQuery(BaseModel):
    updated_since: datetime


class SessionStatusResponse(BaseModel):
    user_id: str
    role: str
    token_expires_at: datetime
    issued_at: datetime | None = None
    jti: str
