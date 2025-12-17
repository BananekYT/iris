import os
import logging
import traceback
import json
from fastapi import FastAPI, HTTPException
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException
from iris.credentials import RsaCredential
from iris.api import IrisHebeApi

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
app.state.registered = False  # flaga gotowości

# ==============================
# FUNKCJE POMOCNICZE
# ==============================
def load_or_create_credential() -> RsaCredential:
    """Wczytaj zapisany credential lub utwórz nowy."""
    if os.path.exists(CREDENTIAL_FILE):
        with open(CREDENTIAL_FILE, "r") as f:
            serialized = f.read()
        credential = RsaCredential.model_validate_json(serialized)
        logger.info("Credential loaded from file")
    else:
        credential = RsaCredential.create_new("Android", "SM-A525F")
        serialized = credential.model_dump_json()
        with open(CREDENTIAL_FILE, "w") as f:
            f.write(serialized)
        logger.info("New credential created and saved to file")
    return credential

# ==============================
# STARTUP / SHUTDOWN
# ==============================
@app.on_event("startup")
async def startup_event():
    if app.state.registered:
        return
    try:
        credential = load_or_create_credential()
        api = IrisHebeApi(credential)
        await api.register_by_token(security_token=TOKEN, pin=PIN, tenant=TENANT)
        client.credential = credential
        await client.register()
        app.state.registered = True
        logger.info("Startup: iris client registered successfully")
    except WrongTokenException as e:
        logger.error("Startup: nieprawidłowy token rejestracyjny lub PIN. Sprawdź ENV.")
        logger.debug("Szczegóły wyjątku: %s", repr(e))
        traceback.print_exc()
    except Exception as e:
        logger.exception("Startup: iris client registration failed")
        traceback.print_exc()

@app.post("/register")
async def register_user(body: dict):
    """
    Rejestruje credential i klienta Iris dla użytkownika.
    Oczekiwane pola JSON:
    {
        "pin": "xxxx",
        "token": "xxxx",
        "tenant": "xxxx"
    }
    """
    pin = body.get("pin")
    token = body.get("token")
    tenant = body.get("tenant")

    if not all([pin, token, tenant]):
        raise HTTPException(status_code=400, detail="Brakuje pola pin/token/tenant")

    try:
        # Tworzymy nowy credential
        credential = RsaCredential.create_new("Android", "SM-A525F")
        api = IrisHebeApi(credential)
        await api.register_by_token(security_token=token, pin=pin, tenant=tenant)
        # Rejestrujemy w kliencie
        client.credential = credential
        await client.register()
        app.state.registered = True
        return {"status": "registered"}
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token lub PIN")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=401, detail="Nieprawidłowy token sesji")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))