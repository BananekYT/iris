from fastapi import FastAPI, HTTPException
from app.iris_client import IrisClient

app = FastAPI()
client = IrisClient()

@app.on_event("startup")
async def startup_event():
    # Rejestracja credentiala przy starcie
    try:
        await client.register()
        print("Startup: iris client registered successfully")
    except Exception as e:
        print("Startup: iris client registration failed:", repr(e))

@app.on_event("shutdown")
async def shutdown_event():
    # Jawne zamknięcie zasobów
    try:
        await client.close()
        print("Shutdown: iris client closed")
    except Exception as e:
        print("Warning: failed to close client on shutdown:", repr(e))

@app.get("/")
async def root():
    return {"message": "Iris service running"}

@app.get("/accounts")
async def get_accounts():
    try:
        accounts = await client.get_accounts()
        # Zwracamy JSON uproszczony do kluczowych informacji
        return [
            {
                "full_name": acc.student_info.full_name,
                "unit_name": acc.unit.name,
                "session_token": acc.session_token,
            }
            for acc in accounts
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/grades")
async def get_grades():
    try:
        accounts = await client.get_accounts()
        if not accounts:
            raise HTTPException(status_code=404, detail="No accounts found")
        student = accounts[0]  # możesz zmienić, którego ucznia chcesz
        grades = await client.get_grades(student.session_token)
        # Zwracamy JSON
        return [g.model_dump() for g in grades]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/exams")
async def get_exams():
    try:
        accounts = await client.get_accounts()
        if not accounts:
            raise HTTPException(status_code=404, detail="No accounts found")
        student = accounts[0]
        exams = await client.get_exams(student.session_token)
        # Zwracamy JSON
        return [e.model_dump() for e in exams]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
