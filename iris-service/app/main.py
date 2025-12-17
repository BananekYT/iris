from fastapi import FastAPI, HTTPException
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException
import logging
import traceback

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
client = IrisClient()

@app.on_event("startup")
async def startup_event():
    # Rejestracja credentiala przy starcie
    try:
        await client.register()
        logger.info("Startup: iris client registered successfully")
    except WrongTokenException as e:
        logger.error("Startup: nieprawidłowy token rejestracyjny (TOKEN). Sprawdź ustawienia w app/config.py")
        logger.debug("Szczegóły wyjątku: %s", repr(e))
        traceback.print_exc()
    except Exception as e:
        logger.exception("Startup: iris client registration failed")
        traceback.print_exc()

@app.on_event("shutdown")
async def shutdown_event():
    # Jawne zamknięcie zasobów
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception as e:
        logger.exception("Warning: failed to close client on shutdown")
        traceback.print_exc()

@app.get("/")
async def root():
    return {"message": "Iris service running"}

@app.get("/accounts")
async def get_accounts():
    try:
        accounts = await client.get_accounts()
        # Zwracamy JSON uproszczony do kluczowych informacji (dostosowane do modelu Account)
        result = []
        for acc in accounts:
            pupil = getattr(acc, "pupil", None)
            full_name = None
            if pupil is not None:
                parts = [getattr(pupil, "first_name", ""), getattr(pupil, "second_name", ""), getattr(pupil, "surname", "")]
                full_name = " ".join([p for p in parts if p])
            unit_name = getattr(getattr(acc, "unit", None), "name", None)
            result.append({
                "full_name": full_name,
                "unit_name": unit_name,
                # session_token nie jest zwracany przez get_accounts() w tej bibliotece;
                # aby uzyskać token sesji, użyj endpointu POST /login
                "session_token": None,
            })
        return result
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token rejestracyjny. Sprawdź konfigurację TOKEN.")
    except RuntimeError as e:
        # runtime errors raised deliberately with user-friendly message
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/raw")
async def get_accounts_raw():
    """Zwraca pełne obiekty `Account.model_dump()` — użyteczne do debugowania pól zwracanych przez API."""
    try:
        accounts = await client.get_accounts()
        return [acc.model_dump() for acc in accounts]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/grades")
async def get_grades(token: str | None = None):
    try:
        # token można podać przez query param `?token=...` lub użyć ostatnio zalogowanego konta
        session_token = token
        if not session_token and getattr(client, "current_account", None):
            session_token = getattr(client.current_account, "session_token", None)
        if not session_token:
            raise HTTPException(status_code=400, detail="Brakuje tokena sesji. Wywołaj POST /login z danymi konta, aby otrzymać `session_token`, lub przekaż go jako query param `?token=...`.")
        grades = await client.get_grades(session_token)
        # Zwracamy JSON
        return [g.model_dump() for g in grades]
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token rejestracyjny. Sprawdź konfigurację TOKEN.")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/exams")
async def get_exams(token: str | None = None):
    try:
        session_token = token
        if not session_token and getattr(client, "current_account", None):
            session_token = getattr(client.current_account, "session_token", None)
        if not session_token:
            raise HTTPException(status_code=400, detail="Brakuje tokena sesji. Wywołaj POST /login z danymi konta, aby otrzymać `session_token`, lub przekaż go jako query param `?token=...`.")
        exams = await client.get_exams(session_token)
        # Zwracamy JSON
        return [e.model_dump() for e in exams]
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token rejestracyjny. Sprawdź konfigurację TOKEN.")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/login")
async def login(body: dict):
    """Zaloguj się danymi konta, zwróć `session_token`.

    Oczekiwane pola JSON: {"login": "username", "password": "pwd", "symbol": "schoolSymbol"}
    """
    try:
        login_val = body.get("login")
        password = body.get("password")
        symbol = body.get("symbol")
        if not all([login_val, password, symbol]):
            raise HTTPException(status_code=400, detail="Brakuje pola login/password/symbol w ciele żądania")
        resp = await client.login(login_val, password, symbol)
        return resp
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WrongTokenException:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token rejestracyjny. Sprawdź konfigurację TOKEN.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
