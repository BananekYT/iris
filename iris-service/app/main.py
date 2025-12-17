import os
import logging
import traceback
from fastapi import FastAPI, HTTPException
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException

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

# ==============================
# TOKEN Z ENV
# ==============================
TOKEN = os.getenv("IRIS_TOKEN")
if not TOKEN:
    logger.warning("Brak IRIS_TOKEN w zmiennych środowiskowych. Sprawdź Railway Environments.")

# ==============================
# STARTUP / SHUTDOWN
# ==============================
@app.on_event("startup")
async def startup_event():
    if getattr(app.state, "registered", False):
        return
    try:
        await client.register()
        app.state.registered = True
        logger.info("Startup: iris client registered successfully")
    except WrongTokenException as e:
        logger.error("Startup: nieprawidłowy token rejestracyjny (TOKEN). Sprawdź ENV IRIS_TOKEN.")
        logger.debug("Szczegóły wyjątku: %s", repr(e))
        traceback.print_exc()
    except Exception as e:
        logger.exception("Startup: iris client registration failed")
        traceback.print_exc()


@app.on_event("shutdown")
async def shutdown_event():
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception as e:
        logger.exception("Warning: failed to close client on shutdown")
        traceback.print_exc()

# ==============================
# HEALTHCHECK
# ==============================
@app.get("/health")
async def health():
    return {"status": "ok"}

# ==============================
# ENDPOINTY
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
                    getattr(pupil, "surname", "")]
                full_name = " ".join([p for p in parts if p])
            unit_name = getattr(getattr(acc, "unit", None), "name", None)
            result.append({
                "full_name": full_name,
                "unit_name": unit_name,
                "session_token": None,  # token sesji przez /login
            })
        return result
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token rejestracyjny. Sprawdź konfigurację IRIS_TOKEN.")
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
        raise HTTPException(status_code=401, detail="Nieprawidłowy token sesji")
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
        raise HTTPException(status_code=401, detail="Nieprawidłowy token sesji")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/login")
async def login(body: dict):
    """Zaloguj się danymi konta, zwróć `session_token`.

    Oczekiwane pola JSON: {"login": "username", "password": "pwd", "symbol": "schoolSymbol"}
    """

    login_val = body.get("login")
    password = body.get("password")
    symbol = body.get("symbol")
    if not all([login_val, password, symbol]):
        raise HTTPException(status_code=400, detail="Brakuje login/password/symbol w ciele żądania")
    try:
        resp = await client.login(login_val, password, symbol)
        return resp
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token sesji")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
