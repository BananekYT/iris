import os
import logging
import traceback
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException
from app.errors import *

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
client = IrisClient()
app.state.registered_users = set()

# ==============================
# SHUTDOWN
# ==============================
@app.on_event("shutdown")
async def shutdown_event():
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception:
        logger.exception("Failed to close client")
        traceback.print_exc()

# ===========================
# EVENT STARTUP
# ===========================
"""
@app.on_event("startup")
async def startup_event():
    # Tutaj kod, który ma się wykonać przy starcie serwera
    print("Serwer startuje...")
    # np. załaduj zapisane credentiale
    from pathlib import Path
    credentials_dir = Path(__file__).resolve().parents[2] / "credentials"
    credentials_dir.mkdir(exist_ok=True)

    if credentials_dir.exists():
        for file in credentials_dir.glob("*.json"):
            user_id = file.stem
            try:
                await client.load_user_credential(user_id)
                app.state.registered_users.add(user_id)
                print(f"Załadowano credentials dla {user_id}")
            except RuntimeError as e:
                print(f"Nie udało się załadować credentials dla {user_id}: {e}")
            except Exception as e:
                print(f"Wystąpił błąd podczas ładowania credentials dla {user_id}: {e}")

"""
# =============================
# OBSŁUGA WŁASNYCH BŁĘDÓW
# =============================
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message
            }
        }
    )

# ==============================
# HEALTHCHECK / READY
# ==============================
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/ready")
async def ready():
    if app.state.registered_users:
        return {"status": "ready", "users": list(app.state.registered_users)}
    else:
        raise HTTPException(status_code=503, detail="Brak załadowanych credentiali")

# ==============================
# ROOT
# ==============================
@app.get("/")
async def root():
    return {"message": "Iris service running"}

# ==============================
# REGISTER
# ==============================
@app.post("/register")
async def register_user(body: dict):
    pin = body.get("pin")
    token = body.get("token")
    tenant = body.get("tenant")
    user_id = body.get("user_id")

    if not all([pin, token, tenant, user_id]):
        raise HTTPException(status_code=400, detail="Brakuje pin/token/tenant/user_id")

    try:
        await client.register(pin, token, tenant, user_id)
        app.state.registered_users.add(user_id)
        return {"status": "registered", "user_id": user_id}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==============================
# ACCOUNTS
# ==============================
@app.get("/accounts")
async def get_accounts(user_id: str):
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
async def get_accounts_raw(user_id: str):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )

    accounts = await client.get_accounts_raw()
    return accounts

# ==============================
# GRADES
# ==============================
@app.get("/grades")
async def get_grades():
    try:
        grades = await client.get_grades()
        return [g.model_dump() for g in grades]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# EXAMS
# ==============================
@app.get("/exams")
async def get_exams():
    try:
        exams = await client.get_exams()
        return [e.model_dump() for e in exams]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# SZCZĘŚLIWY NUMEREK
# =============================
@app.get("/lucky-number")
async def lucky_number():
    try:
        lucky = await client.get_lucky_number()
        return [g.model_dump() for g in lucky]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))