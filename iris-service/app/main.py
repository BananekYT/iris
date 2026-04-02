import os
import logging
from collections import defaultdict, deque
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from typing import Any
from time import monotonic
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.iris_client import IrisClient
from app.auth import (
    create_token_pair,
    get_current_claims,
    get_current_user,
    revoke_access_token,
    revoke_all_refresh_tokens_for_user,
    revoke_refresh_token,
    refresh_token_pair,
    try_get_subject_from_auth_header,
)
from app.errors import *
from app.models import (
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    SelectAccountRequest,
    SessionStatusResponse,
    TokenPairResponse,
)

# ==============================
# LOGOWANIE
# ==============================
ENV = os.getenv("ENV", "development")
level = logging.DEBUG if ENV != "production" else logging.INFO
logging.basicConfig(level=level)
logger = logging.getLogger(__name__)

# ==============================
# APLIKACJA I KLIENT
# ==============================
app = FastAPI()
_request_client: ContextVar[IrisClient | None] = ContextVar("request_client", default=None)
app.state.selected_account_by_user: dict[str, int] = {}

REGISTER_RATE_LIMIT = 5
REGISTER_WINDOW_SECONDS = 60
_register_attempts: dict[str, deque[float]] = defaultdict(deque)

GLOBAL_RATE_LIMIT = int(os.getenv("GLOBAL_RATE_LIMIT", "120"))
GLOBAL_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("GLOBAL_RATE_LIMIT_WINDOW_SECONDS", "60"))
_global_attempts: dict[str, deque[float]] = defaultdict(deque)

ENABLE_UNVERSIONED_API = os.getenv("ENABLE_UNVERSIONED_API", "true").lower() == "true"
UNVERSIONED_SUNSET_DATE = os.getenv("UNVERSIONED_SUNSET_DATE", "2026-12-31")
ENV = os.getenv("ENV", "development").lower()

cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
CORS_ALLOWED_ORIGINS = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    expose_headers=["X-Request-Id", "Deprecation", "Sunset", "Link"],
)


class RequestScopedIrisClient:
    def _get_client(self) -> IrisClient:
        request_client = _request_client.get()
        if request_client is None:
            raise RuntimeError("Brak kontekstu klienta dla bieżącego żądania")
        return request_client

    def __getattr__(self, name: str):
        return getattr(self._get_client(), name)


client = RequestScopedIrisClient()


def resolve_request_user_id(
    token_user_id: str = Depends(get_current_user),
) -> str:
    return token_user_id


def _error_payload(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def _paginate(items: list[Any], limit: int, offset: int) -> dict:
    total = len(items)
    safe_offset = max(offset, 0)
    safe_limit = min(max(limit, 1), 200)
    sliced = items[safe_offset:safe_offset + safe_limit]
    return {
        "items": sliced,
        "pagination": {
            "total": total,
            "offset": safe_offset,
            "limit": safe_limit,
        },
    }


def _sort_items(items: list[dict], sort: str) -> list[dict]:
    reverse = sort.lower() == "desc"

    def _sort_key(item: dict):
        ts = _extract_item_timestamp(item)
        if ts is not None:
            return ts
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(items, key=_sort_key, reverse=reverse)


def _parse_datetime(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _extract_item_timestamp(item: dict) -> datetime | None:
    for key, value in item.items():
        if isinstance(value, str) and ("date" in key.lower() or "time" in key.lower()):
            parsed = _parse_datetime(value)
            if parsed is not None:
                return parsed
    return None


def _filter_delta(items: list[dict], updated_since: datetime) -> list[dict]:
    result: list[dict] = []
    if updated_since.tzinfo is None:
        updated_since = updated_since.replace(tzinfo=timezone.utc)
    for item in items:
        ts = _extract_item_timestamp(item)
        if ts is not None and ts >= updated_since:
            result.append(item)
    return result


def _enforce_roles(claims: dict, allowed_roles: set[str]) -> None:
    role = str(claims.get("role", "Uczen"))
    if role not in allowed_roles:
        raise HTTPException(
            status_code=403,
            detail="Brak uprawnień do tego zasobu",
        )


def _parse_average_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        if not normalized:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _scope_kind(scope: str | None) -> str:
    if not scope:
        return "unknown"
    lowered = str(scope).strip().lower()
    if lowered in {"pupil", "student", "self", "uczen", "uczeń"}:
        return "pupil"
    if lowered in {"class", "clazz", "oddzial", "oddział", "klasa"}:
        return "class"
    return "unknown"


@app.middleware("http")
async def request_client_middleware(request: Request, call_next):
    request_client = IrisClient()
    token_user = try_get_subject_from_auth_header(request.headers.get("authorization"))
    if token_user:
        preferred = app.state.selected_account_by_user.get(token_user)
        request_client.set_preferred_pupil_id(preferred)
    token = _request_client.set(request_client)
    try:
        return await call_next(request)
    finally:
        try:
            await request_client.close()
        except Exception:
            logger.exception("Failed to close request iris client")
        _request_client.reset(token)


@app.middleware("http")
async def versioning_and_security_middleware(request: Request, call_next):
    original_path = request.scope.get("path", "")
    if original_path.startswith("/v1"):
        request.scope["path"] = original_path[3:] or "/"

    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    request.state.request_id = request_id
    request.state.started_at = monotonic()

    # Globalny rate limit (poza endpointami technicznymi).
    skip_rate_limit_paths = {"/health", "/ready"}
    effective_path = request.scope.get("path", original_path)
    if effective_path not in skip_rate_limit_paths:
        ip = request.client.host if request.client else "unknown"
        now = monotonic()
        key = f"{ip}:{effective_path}"
        attempts = _global_attempts[key]
        while attempts and now - attempts[0] > GLOBAL_RATE_LIMIT_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= GLOBAL_RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content=_error_payload(
                    code="RATE_LIMIT_EXCEEDED",
                    message="Zbyt wiele żądań. Spróbuj ponownie za chwilę.",
                    details={"request_id": request_id},
                ),
                headers={"X-Request-Id": request_id},
            )
        attempts.append(now)

    response = await call_next(request)

    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    if not original_path.startswith("/v1"):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = UNVERSIONED_SUNSET_DATE
        response.headers["Link"] = f"</v1{original_path}>; rel=\"successor-version\""
        if not ENABLE_UNVERSIONED_API and original_path not in {"/health", "/ready"}:
            return JSONResponse(
                status_code=410,
                content=_error_payload(
                    code="UNVERSIONED_API_DEPRECATED",
                    message="Użyj wersjonowanego endpointu /v1",
                    details={"request_id": request_id},
                ),
                headers={"X-Request-Id": request_id},
            )

    duration_ms = round((monotonic() - request.state.started_at) * 1000, 2)
    auth_header = request.headers.get("authorization")
    token_user = try_get_subject_from_auth_header(auth_header)
    client_ip = request.client.host if request.client else "unknown"
    logger.info(
        "audit request_id=%s ip=%s method=%s path=%s status=%s user_id=%s duration_ms=%s",
        request_id,
        client_ip,
        request.method,
        original_path,
        response.status_code,
        token_user or "-",
        duration_ms,
    )
    return response


def enforce_register_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = monotonic()
    attempts = _register_attempts[ip]

    while attempts and now - attempts[0] > REGISTER_WINDOW_SECONDS:
        attempts.popleft()

    if len(attempts) >= REGISTER_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Zbyt wiele prób rejestracji. Spróbuj ponownie za chwilę.",
        )

    attempts.append(now)


async def load_user_context(user_id: str) -> None:
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register",
        )

    selected_pupil_id = app.state.selected_account_by_user.get(user_id)
    if selected_pupil_id is not None:
        selected = await client.select_current_account(selected_pupil_id)
        if not selected:
            app.state.selected_account_by_user.pop(user_id, None)

# =============================
# OBSŁUGA WŁASNYCH BŁĘDÓW
# =============================
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            code=exc.code,
            message=exc.message,
            details={"request_id": getattr(request.state, "request_id", None)},
        ),
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        logger.error("HTTP 500 response detail hidden from client: %s", exc.detail)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                code="INTERNAL_SERVER_ERROR",
                message="Wewnętrzny błąd serwera",
                details={"request_id": getattr(request.state, "request_id", None)},
            ),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            code=f"HTTP_{exc.status_code}",
            message=str(exc.detail),
            details={"request_id": getattr(request.state, "request_id", None)},
        ),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception")
    return JSONResponse(
        status_code=500,
        content=_error_payload(
            code="UNHANDLED_EXCEPTION",
            message="Wewnętrzny błąd serwera",
            details={"request_id": getattr(request.state, "request_id", None)},
        ),
    )

# ==============================
# HEALTHCHECK + READY
# ==============================
@app.get("/health")
async def health():
    return {"code": "HEALTH_CHECK", "status": "ok"}

@app.get("/ready")
async def ready():
    return {"status": "ready"}

# ==============================
# ROOT
# ==============================
@app.get("/")
async def root():
    return {"code": "API_INFO", "message": "Wulkaniczny Dzienniczek API - running"}

# ==============================
# REGISTER
# ==============================
@app.post("/register")
async def register_user(body: RegisterRequest, request: Request):
    enforce_register_rate_limit(request)

    try:
        resolved_user_id = await client.register(
            body.pin,
            body.token,
            body.tenant,
            device_name=body.device_name,
            device_model=body.device_model,
        )
        await load_user_context(resolved_user_id)
        role = await client.get_current_role()
        token_pair = create_token_pair(user_id=resolved_user_id, role=role)
        return {
            "status": "registered",
            "user_id": resolved_user_id,
            **token_pair,
        }
    except RuntimeError as exc:
        logger.exception(
            "Register failed request_id=%s pin=%s tenant=%s",
            getattr(request.state, "request_id", None),
            body.pin,
            body.tenant,
        )
        raise HTTPException(
            status_code=400,
            detail="Rejestracja nie powiodła się. Sprawdź dane i spróbuj ponownie.",
        )


@app.post("/auth/refresh", response_model=TokenPairResponse)
async def auth_refresh(body: RefreshRequest):
    return refresh_token_pair(body.refresh_token)


@app.post("/auth/logout")
async def auth_logout(
    body: LogoutRequest,
    claims: dict = Depends(get_current_claims),
):
    revoke_access_token(str(claims.get("jti")))
    if body.refresh_token:
        revoke_refresh_token(body.refresh_token)
    if body.all_sessions:
        revoke_all_refresh_tokens_for_user(str(claims["sub"]))
    return {"status": "logged_out"}


@app.get("/auth/session", response_model=SessionStatusResponse)
async def auth_session_status(claims: dict = Depends(get_current_claims)):
    exp_ts = int(claims["exp"])
    iat_ts = int(claims["iat"])
    return {
        "user_id": str(claims["sub"]),
        "role": str(claims.get("role", "Uczen")),
        "token_expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc),
        "issued_at": datetime.fromtimestamp(iat_ts, tz=timezone.utc),
        "jti": str(claims["jti"]),
    }


@app.get("/me")
async def me(
    user_id: str = Depends(resolve_request_user_id),
):
    await load_user_context(user_id)
    return await client.get_profile()


@app.post("/accounts/select")
async def select_active_account(
    body: SelectAccountRequest,
    user_id: str = Depends(resolve_request_user_id),
):
    await load_user_context(user_id)
    selected = await client.select_current_account(body.pupil_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Nie znaleziono konta o podanym pupil_id")
    app.state.selected_account_by_user[user_id] = body.pupil_id
    return {"status": "ok", "active_pupil_id": body.pupil_id}


@app.get("/glossary")
async def glossary():
    return {
        "roles": ["Uczen", "Rodzic", "Opiekun", "Nauczyciel", "Dyrektor"],
        "message_boxes": ["INBOX", "SENT", "ARCHIVE", "TRASH", "DRAFT"],
        "token_types": ["access", "refresh"],
    }

# ==============================
# ACCOUNTS
# ==============================
@app.get("/accounts")
async def get_accounts(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        accounts = await client.get_accounts()
        result = []

        for acc in accounts:
            pupil = acc.pupil
            journal = getattr(acc, "journal", None)
            login = getattr(acc, "login", {}) or {}
            unit = getattr(acc, "unit", None)

            # Pełne imię i nazwisko
            full_name = " ".join(
                p for p in [
                    login.get("FirstName", ""),
                    login.get("SecondName", ""),
                    login.get("Surname", "")
                ] if p
            )

            result.append({
                "class_display": getattr(acc, "class_display", None),
                "pupil_number": getattr(journal, "pupil_number", None),
                "pupil_id": getattr(pupil, "id", None),
                "login_id": login.get("Id"),
                "email": login.get("Value"),
                "first_name": login.get("FirstName"),
                "second_name": login.get("SecondName"),
                "surname": login.get("Surname"),
                "display_name": login.get("DisplayName"),
                "login_role": login.get("LoginRole"),
                "unit_name": getattr(unit, "name", None),
                "unit_city": getattr(unit, "city", None) or extract_city_from_display_name(getattr(unit, "display_name", "")),
                "unit_symbol": getattr(unit, "symbol", None),
                "unit_short": getattr(unit, "short", None),
                "unit_rest_url": getattr(unit, "rest_url", None)
            })


        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# Pomocnicza funkcja do wyciągania miejscowości ze display_name szkoły
def extract_city_from_display_name(display_name: str) -> str | None:
    """
    Jeśli w display_name jest np. 'Publiczna Szkoła Podstawowa im. Jana Pawła II w Gdańsku',
    zwraca 'Gdańsk'.
    """
    if " w " in display_name:
        return display_name.split(" w ")[-1].strip()
    return None


@app.get("/accounts/raw")
async def get_accounts_raw(
    user_id: str = Depends(resolve_request_user_id),
    claims: dict = Depends(get_current_claims),
):
    if ENV == "production":
        raise HTTPException(status_code=404, detail="Endpoint niedostępny")

    _enforce_roles(claims, {"Rodzic", "Opiekun", "Nauczyciel", "Dyrektor"})
    await load_user_context(user_id)

    accounts = await client.get_accounts_raw()
    return accounts

# ==============================
# Podsumowanie kont (ilosc uczniow)
# ==============================

LOGIN_ROLE_MAP = {
    "Uczen": "Uczeń",
    "Rodzic": "Rodzic",
    "Opiekun": "Opiekun",
    "Nauczyciel": "Nauczyciel",
    "Dyrektor": "Dyrektor",
}


@app.get("/account/summary")
async def account_summary(user_id: str = Depends(resolve_request_user_id)):
    """
    Zwraca podsumowanie konta:
    - liczba uczniów
    - liczba szkół
    - dane logującego
    - szkoły z przypisanymi uczniami
    """

    # 1️⃣ Załaduj credentials
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail="Nie znaleziono credentials. Najpierw wywołaj /register."
        )

    # 2️⃣ Pobierz konta (accounts)
    try:
        accounts = await client.get_accounts()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    schools_map = {}
    students_ids = set()
    login_info = None

    # 3️⃣ Przetwarzanie kont
    for acc in accounts:
        pupil = acc.pupil
        journal = getattr(acc, "journal", None)
        unit = getattr(acc, "unit", None)
        login = getattr(acc, "login", {}) or {}

        # zapamiętujemy login (ten sam dla wszystkich)
        if not login_info:
            role_raw = login.get("LoginRole")
            login_info = {
                "login_id": login.get("Id"),
                "email": login.get("Value"),
                "first_name": login.get("FirstName"),
                "second_name": login.get("SecondName"),
                "surname": login.get("Surname"),
                "display_name": login.get("DisplayName"),
                "role": LOGIN_ROLE_MAP.get(role_raw, role_raw),
            }

        # klucz szkoły
        school_key = getattr(unit, "id", None) or getattr(unit, "symbol", None)
        if not school_key:
            continue

        if school_key not in schools_map:
            schools_map[school_key] = {
                "school_id": getattr(unit, "id", None),
                "school_name": getattr(unit, "name", None),
                "city": getattr(unit, "city", None)
                        or extract_city_from_display_name(getattr(unit, "display_name", "")),
                "symbol": getattr(unit, "symbol", None),
                "students": []
            }

        # uczeń
        pupil_id = getattr(pupil, "id", None)
        students_ids.add(pupil_id)

        role_raw = login.get("LoginRole")

        schools_map[school_key]["students"].append({
            "id": pupil_id,
            "first_name": login.get("FirstName"),
            "surname": login.get("Surname"),
            "class": getattr(acc, "class_display", None),
            "role": LOGIN_ROLE_MAP.get(role_raw, role_raw),
        })

    # 4️⃣ Finalny response
    response = {
        "students_count": len(students_ids),
        "schools_count": len(schools_map),
        "login": login_info,
        "schools": list(schools_map.values())
    }

    return response

# ==============================
# OCENY
# ==============================
@app.get("/grades")
async def get_grades(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        grades = await client.get_grades()
        return [g.model_dump() for g in grades]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# ŚREDNIA OCEN
# =============================
@app.get("/grades-averages")
async def get_grades_averages(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        averages = await client.get_grades_averages()
        return [a.model_dump() for a in averages]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/student-vs-class")
async def get_student_vs_class(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )

    try:
        accounts = await client.get_accounts()
        averages = await client.get_grades_averages()

        current = client.current_account or (accounts[0] if accounts else None)
        class_display = getattr(current, "class_display", None)
        pupil_id = getattr(getattr(current, "pupil", None), "id", None)

        by_subject: dict[int, dict] = {}
        for item in averages:
            dumped = item.model_dump()
            subject = dumped.get("subject") or {}
            subject_id = subject.get("id")
            if not subject_id:
                continue

            row = by_subject.setdefault(
                subject_id,
                {
                    "subject_id": subject_id,
                    "subject_name": subject.get("name"),
                    "pupil_average": None,
                    "class_average": None,
                },
            )

            parsed_avg = _parse_average_value(dumped.get("average"))
            kind = _scope_kind(dumped.get("scope"))

            if kind == "pupil":
                row["pupil_average"] = parsed_avg
            elif kind == "class":
                row["class_average"] = parsed_avg
            elif row["pupil_average"] is None:
                # fallback, gdy scope nie jest jednoznaczne
                row["pupil_average"] = parsed_avg

        subjects = []
        above = below = equal = comparable = 0
        pupil_values = []
        class_values = []

        for row in by_subject.values():
            pa = row["pupil_average"]
            ca = row["class_average"]
            delta = None
            standing = "unknown"

            if pa is not None:
                pupil_values.append(pa)
            if ca is not None:
                class_values.append(ca)

            if pa is not None and ca is not None:
                delta = round(pa - ca, 2)
                comparable += 1
                if delta > 0:
                    standing = "above_class"
                    above += 1
                elif delta < 0:
                    standing = "below_class"
                    below += 1
                else:
                    standing = "equal_class"
                    equal += 1

            subjects.append(
                {
                    **row,
                    "delta": delta,
                    "standing": standing,
                }
            )

        pupil_overall = round(sum(pupil_values) / len(pupil_values), 2) if pupil_values else None
        class_overall = round(sum(class_values) / len(class_values), 2) if class_values else None
        overall_delta = (
            round(pupil_overall - class_overall, 2)
            if pupil_overall is not None and class_overall is not None
            else None
        )

        return {
            "pupil_id": pupil_id,
            "class_display": class_display,
            "summary": {
                "subjects_total": len(subjects),
                "subjects_comparable": comparable,
                "above_class": above,
                "below_class": below,
                "equal_class": equal,
                "pupil_overall_average": pupil_overall,
                "class_overall_average": class_overall,
                "overall_delta": overall_delta,
            },
            "subjects": sorted(
                subjects,
                key=lambda x: (x.get("subject_name") or "").lower(),
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
     
# =============================
# OCENY ŚRÓDROCZNE I KOŃCOWOROCZNE
# ===========================
@app.get("/grades-summary")
async def get_grades_summary(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        grades_summary = await client.get_grades_summary()
        return [gs.model_dump() for gs in grades_summary]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 

# ==============================
# SPRAWDZIANY
# ==============================
@app.get("/exams")
async def get_exams(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        exams = await client.get_exams()
        return [e.model_dump() for e in exams]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# SZCZĘŚLIWY NUMEREK
# ==============================
@app.get("/lucky-number")
async def lucky_number(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        lucky = await client.get_lucky_number()
        # Zwracamy pojedynczy obiekt jako słownik
        return lucky.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# FREKWENCJA
# =============================
@app.get("/attendance")
async def get_attendance(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        attendance = await client.get_attendance()
        return [a.model_dump() for a in attendance]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# FREKWENCJA DODATKOWA (usprawiedliwienia, dodatkowe nieobecności)
# =====================================
@app.get("/attendance/extra")
async def get_attendance_extra_info(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        attendance_extra_info = await client.get_presence_extra()
        return [a.model_dump() for a in attendance_extra_info]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# SZCZEGÓŁY FREKWENCJI DODATKOWEJ
# =====================================
@app.get("/attendance/extra-info/{info_id}")
async def get_attendance_extra_info_details(info_id: int, user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        details = await client.get_presence_extra_info(info_id)
        return details.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =====================================
# STATYSTYKI MIESIĘCZNE FREKWENCJI
# =====================================
@app.get("/attendance/month-stats")
async def get_attendance_month_stats(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        stats = await client.get_presence_month_stats()
        return [s.model_dump() for s in stats]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# STATYSTYKI FREKWENCJI PER PRZEDMIOT
# =====================================
@app.get("/attendance/subject-stats")
async def get_attendance_subject_stats(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        stats = await client.get_presence_subject_stats()
        return [s.model_dump() for s in stats]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =============================
# PLAN LEKCJI
# ============================
@app.get("/timetable")
async def get_timetable(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        timetable = await client.get_schedule()
        return [t.model_dump() for t in timetable]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =============================
# PLAN LEKCJI DODATKOWY / ZMIANY
# ============================
@app.get("/timetable/extra")
async def get_timetable_extra(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        timetable_extra = await client.get_schedule_extra()
        return [te.model_dump() for te in timetable_extra]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =============================
# PLANOWANE LEKCJE
# =============================
@app.get("/planned-lessons")
async def get_planned_lessons(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        timetable_planned = await client.get_planned_lessons()
        return [tp.model_dump() for tp in timetable_planned]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ==============================
# NAUCZYCIELE
# =============================
@app.get("/teachers")
async def get_teachers(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        teachers = await client.get_teachers()
        return [t.model_dump() for t in teachers]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ===============================
# SZKOŁA - INFORMACJE
# =============================
@app.get("/school-info")
async def get_school_info(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        school_info = await client.get_school_info()
        return [s.model_dump() for s in school_info]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# UWAGI / POCHWAŁY
# =============================
@app.get("/notes")
async def get_notes(
    user_id: str = Depends(resolve_request_user_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        notes = await client.get_notes()
        dumped = [n.model_dump() for n in notes]
        sorted_items = _sort_items(dumped, sort)
        return _paginate(sorted_items, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/notes/delta")
async def get_notes_delta(
    updated_since: datetime,
    user_id: str = Depends(resolve_request_user_id),
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        notes = await client.get_notes()
        dumped = [n.model_dump() for n in notes]
        return _filter_delta(dumped, updated_since)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# PRZERWY W NAUCE
# =====================================
@app.get("/vacations")
async def get_vacations(user_id: str = Depends(resolve_request_user_id), date_from: date = None, date_to: date = None):
    """Endpoint pobierający wakacje/przerwy ucznia w określonym przedziale dat"""
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    
    # Ustawienie domyślnych dat, jeśli nie podano
    if date_from is None:
        date_from = date.today()
    if date_to is None:
        date_to = date_from + timedelta(days=365)  # np. rok do przodu

    try:
        vacations = await client.get_vacations(date_from=date_from, date_to=date_to)
        return [v.model_dump() for v in vacations]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# ZADANIA DOMOWE
# =====================================
@app.get("/homework")
async def get_homework(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        homework = await client.get_homework()
        return [h.model_dump() for h in homework]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# ZEBRANIA
# =====================================
@app.get("/meetings")
async def get_meetings(user_id: str = Depends(resolve_request_user_id), date_from: date = None, date_to: date = None):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    
    # Ustawienie domyślnych dat, jeśli nie podano
    if date_from is None:
        date_from = date.today()
    if date_to is None:
        date_to = date_from + timedelta(days=365)  # np. rok do przodu

    try:
        meetings = await client.get_meetings()
        return [m.model_dump() for m in meetings]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# =====================================
# OGŁOSZENIA
# =====================================
@app.get("/announcements")
async def get_announcements(
    user_id: str = Depends(resolve_request_user_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        announcements = await client.get_announcements()
        dumped = [a.model_dump() for a in announcements]
        sorted_items = _sort_items(dumped, sort)
        return _paginate(sorted_items, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/announcements/delta")
async def get_announcements_delta(
    updated_since: datetime,
    user_id: str = Depends(resolve_request_user_id),
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        announcements = await client.get_announcements()
        dumped = [a.model_dump() for a in announcements]
        return _filter_delta(dumped, updated_since)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ==============================
# POSIŁKI (MEALS)
# =============================
@app.get("/meals")
async def get_meals(user_id: str = Depends(resolve_request_user_id), date_from: date = None, date_to: date = None, full: bool = False):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    
    # Ustawienie domyślnych dat, jeśli nie podano
    if date_from is None:
        date_from = date.today()
    if date_to is None:
        date_to = date_from + timedelta(days=365)  # np. rok do przodu

    try:
        meals = await client.get_meals(date_from=date_from, date_to=date_to, full=full)
        if meals is None:
            return []  # Zwracamy pustą listę, żeby nie wysypało się JSONem
        return [m.model_dump() for m in meals]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# /---- WIADMOŚCI ----/ #    
# ==============================
# OTRZYMANE WIADOMOŚCI
# =============================
@app.get("/messages/received")
async def get_received_messages(
    user_id: str = Depends(resolve_request_user_id),
    box: str = "INBOX",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        messages = await client.get_received_messages(box=box)
        dumped = [m.model_dump() for m in messages] if messages else []
        sorted_items = _sort_items(dumped, sort)
        return _paginate(sorted_items, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/messages/received/delta")
async def get_received_messages_delta(
    updated_since: datetime,
    user_id: str = Depends(resolve_request_user_id),
    box: str = "INBOX",
):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        messages = await client.get_received_messages(box=box)
        dumped = [m.model_dump() for m in messages] if messages else []
        return _filter_delta(dumped, updated_since)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
