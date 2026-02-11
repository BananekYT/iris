import os
import logging
import traceback
from collections import defaultdict
from datetime import date, timedelta
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from app.iris_client import IrisClient
from iris._exceptions import WrongTokenException
from app.auth import create_access_token, get_current_user_optional
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


def resolve_request_user_id(
    user_id: str | None = Query(default=None),
    token_user_id: str | None = Depends(get_current_user_optional),
) -> str:
    if user_id:
        return user_id
    if token_user_id:
        return token_user_id
    raise HTTPException(
        status_code=401,
        detail="Brak user_id. Podaj user_id w query albo użyj Bearer token z /register.",
    )

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
# HEALTHCHECK + READY
# ==============================
@app.get("/health")
async def health():
    return {"code": "HEALTH_CHECK", "status": "ok"}

@app.get("/ready")
async def ready(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    if app.state.registered_users:
        return {"status": "ready", "users": list(app.state.registered_users)}
    else:
        raise HTTPException(status_code=503, detail="Brak załadowanych credentiali")

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
async def register_user(body: dict):
    pin = body.get("pin")
    token = body.get("token")
    tenant = body.get("tenant")
    user_id = body.get("user_id")

    if not all([pin, token, tenant]):
        raise HTTPException(status_code=400, detail="Brakuje pin/token/tenant")

    try:
        resolved_user_id = await client.register(pin, token, tenant, user_id)
        app.state.registered_users.add(resolved_user_id)
        access_token = create_access_token(resolved_user_id)
        return {"status": "registered", "user_id": resolved_user_id, "access_token": access_token}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
async def get_accounts_raw(user_id: str = Depends(resolve_request_user_id)):
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
async def get_notes(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        notes = await client.get_notes()
        return [n.model_dump() for n in notes]
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
async def get_announcements(user_id: str = Depends(resolve_request_user_id)):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        announcements = await client.get_announcements()
        return [a.model_dump() for a in announcements]
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
async def get_received_messages(user_id: str = Depends(resolve_request_user_id), box: str = "INBOX"):
    try:
        await client.load_user_credential(user_id)
    except RuntimeError:
        raise HTTPException(
            status_code=404,
            detail=f"Nie udało się załadować credentials dla {user_id}. Najpierw wywołaj /register"
        )
    try:
        messages = await client.get_received_messages(box=box)
        return [m.model_dump() for m in messages] if messages else []
    except Exception as e:
        boxes = ["INBOX", "SENT", "ARCHIVE", "TRASH", "DRAFT"]
        for box in boxes:
            messages = await client.get_received_messages(box=box)
            print(box, len(messages))
        raise HTTPException(status_code=500, detail=str(e))
