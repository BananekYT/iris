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
# STARTUP / SHUTDOWN
# ==============================
@app.on_event("shutdown")
async def shutdown_event():
    try:
        await client.close()
        logger.info("Shutdown: iris client closed")
    except Exception:
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
# HEALTHCHECK / READY
# ==============================
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/ready")
async def ready():
    if app.state.registered_users:
        return {"status": "ready"}
    raise HTTPException(status_code=503, detail="Iris client not registered yet")

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
async def get_accounts():
    try:
        accounts = await client.get_accounts()
        result = []

        for acc in accounts:
            pupil = acc.pupil
            full_name = " ".join(
                p for p in [
                    pupil.first_name,
                    pupil.second_name,
                    pupil.surname
                ] if p
            )

            result.append({
                "full_name": full_name,
                "unit_name": acc.unit.name
            })

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/accounts/raw")
async def get_accounts_raw():
    try:
        return await client.get_accounts_raw()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
