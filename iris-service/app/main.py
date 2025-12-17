# app/main.py
import os
import logging
import traceback
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException
from pathlib import Path
from app.errors import *


# katalog główny projektu
#PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent  # katalog nadrzędny ponad app/

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
# STARTUP / SHUTDOWN
# ==============================
@app.on_event("shutdown")
async def shutdown_event():
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception as e:
        logger.exception("Failed to close client")
        traceback.print_exc()


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
# ENDPOINTY API
# ==============================
@app.post("/register")
async def register_user(body: dict):
    """
    Rejestruje credential i klienta Iris dla użytkownika.
    JSON:
    {
        "pin": "xxxx",
        "token": "xxxx",
        "tenant": "xxxx",
        "user_id": "unique_user_id"
    }
    """
    pin = body.get("pin")
    token = body.get("token")
    tenant = body.get("tenant")
    user_id = body.get("user_id")

    if not all([pin, token, tenant, user_id]):
        raise HTTPException(status_code=400, detail="Brakuje pola pin/token/tenant/user_id")

    try:
        await client.register(pin, token, tenant, user_id)
        app.state.registered_users.add(user_id)
        return {"status": "registered", "user_id": user_id}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WrongTokenException:
        raise WrongTokenError("Nieprawidłowy token sesji")

@app.on_event("shutdown")
async def shutdown_event():
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception as e:
        logger.exception("Warning: failed to close client on shutdown")
        traceback.print_exc()

# ==============================
# HEALTHCHECK / READY
# ==============================
@app.get("/health")
async def health():
    """Lekkie sprawdzenie, czy serwis działa."""
    return {"status": "ok"}

@app.get("/ready")
async def ready():
    """Sprawdza, czy klient Iris został zarejestrowany przez użytkownika."""
    if app.state.registered:
        return {"status": "ready"}
    else:
        raise HTTPException(status_code=503, detail="Iris client not registered yet")

# ==============================
# ENDPOINTY API
# ==============================
@app.get("/")
async def root():
    return {"message": "Iris service running"}

@app.get("/accounts")
async def get_accounts():
    try:
        accounts = await client.get_accounts()
        result = []
        for acc in accounts:
            pupil = getattr(acc, "pupil", None)
            full_name = None
            if pupil:
                parts = [
                    getattr(pupil, "first_name", ""),
                    getattr(pupil, "second_name", ""),
                    getattr(pupil, "surname", "")
                ]
                full_name = " ".join([p for p in parts if p])
            unit_name = getattr(getattr(acc, "unit", None), "name", None)
            result.append({
                "full_name": full_name,
                "unit_name": unit_name,
                "session_token": None
            })
        return result
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token sesji")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/accounts/raw")
async def get_accounts_raw():
    try:
        accounts = await client.get_accounts()
        return [acc.model_dump() for acc in accounts]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/grades")
async def get_grades(token: str | None = None):
    try:
        session_token = token or getattr(client.current_account, "session_token", None)
        if not session_token:
            raise HTTPException(
                status_code=400,
                detail="Brakuje tokena sesji. Wywołaj POST /login lub przekaż ?token=..."
            )
        grades = await client.get_grades(session_token)
        return [g.model_dump() for g in grades]
    except WrongTokenException:
        raise WrongTokenError("Nieprawidłowy token sesji")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/exams")
async def get_exams(token: str | None = None):
    try:
        session_token = token or getattr(client.current_account, "session_token", None)
        if not session_token:
            raise HTTPException(
                status_code=400,
                detail="Brakuje tokena sesji. Wywołaj POST /login lub przekaż ?token=..."
            )
        exams = await client.get_exams(session_token)
        return [e.model_dump() for e in exams]
    except WrongTokenException:
        raise WrongTokenError("Nieprawidłowy token sesji")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/login")
async def login(body: dict):
    login_val = body.get("login")
    password = body.get("password")
    symbol = body.get("symbol")
    if not all([login_val, password, symbol]):
        raise HTTPException(status_code=400, detail="Brakuje login/password/symbol/user_id w ciele żądania")
    try:
        resp = await client.login(login_val, password, symbol)
        return resp
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WrongTokenException:
        raise WrongTokenError("Nieprawidłowy token sesji")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/switch-account")
async def switch_account(body: dict):
    symbol = body.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Brakuje symbol w ciele żądania")
    try:
        await client.switch_account(symbol)
        return {"status": "switched", "symbol": symbol}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WrongTokenException:
        raise WrongTokenError("Nieprawidłowy token sesji")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))