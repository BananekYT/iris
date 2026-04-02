from iris.credentials import RsaCredential
from iris.api import IrisHebeApi
from .secure_credential import save_credential, load_credential

from datetime import date, timedelta
from iris._exceptions import WrongTokenException, UsedTokenException
from .errors import CredentialNotFoundError
import asyncio
import aiohttp
import inspect
import uuid
from pathlib import Path

# ROOT_DIR = katalog nadrzędny dla katalogu "iris-services"
ROOT_DIR = Path(__file__).resolve().parents[2]

# katalog credentials w root
CREDENTIALS_DIR = ROOT_DIR / "credentials"
CREDENTIALS_DIR.mkdir(exist_ok=True, parents=True)


class IrisClient:
    def __init__(self):
        self.device_name = "Android"
        self.device_model = "SM-A525F"

        self.credential: RsaCredential | None = None
        self.api: IrisHebeApi | None = None
        self.current_account = None

    # =====================================
    # API INIT
    # =====================================
    def _ensure_api(self):
        """
        API może zostać utworzone WYŁĄCZNIE,
        jeśli credential zostało wcześniej:
        - zarejestrowane (register)
        - albo wczytane z dysku (load_user_credential)

        Nie tworzymy credential "w ciemno",
        bo powoduje to błędy typu: None/mobile/register/hebe
        """
        if self.api is None:
            if self.credential is None:
                raise CredentialNotFoundError(
                    "Brak credential. Najpierw wywołaj register() "
                    "lub load_user_credential(user_id)."
                )
            self.api = IrisHebeApi(self.credential)
        return self.api

    # =====================================
    # CLEANUP
    # =====================================
    async def _close_api_if_needed(self):
        if self.api is not None:
            close_fn = getattr(self.api, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res

        def _collect_sessions(obj, _visited=None):
            sessions = []
            if obj is None:
                return sessions
            if _visited is None:
                _visited = set()

            if id(obj) in _visited:
                return sessions
            _visited.add(id(obj))

            if isinstance(obj, aiohttp.ClientSession):
                return [obj]

            if isinstance(obj, (list, tuple, set)):
                for i in obj:
                    sessions.extend(_collect_sessions(i, _visited))
            elif isinstance(obj, dict):
                for v in obj.values():
                    sessions.extend(_collect_sessions(v, _visited))
            else:
                for name in dir(obj):
                    if name.startswith("__"):
                        continue
                    try:
                        attr = getattr(obj, name)
                    except Exception:
                        continue
                    if inspect.isroutine(attr) or inspect.ismodule(attr):
                        continue
                    sessions.extend(_collect_sessions(attr, _visited))

            return sessions

        for sess in _collect_sessions(self.api) + _collect_sessions(self.credential):
            try:
                res = sess.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        self.api = None

    async def close(self):
        await self._close_api_if_needed()

    # =====================================
    # REGISTER (JEDNORAZOWE)
    # =====================================
    async def register(
        self,
        pin: str,
        token: str,
        tenant: str,
        user_id: str | None = None,
    ) -> str:
        # tworzymy NOWE credential tylko tutaj
        self.credential = RsaCredential.create_new(
            self.device_name,
            self.device_model
        )
        self.api = IrisHebeApi(self.credential)

        try:
            await self.api.register_by_token(
                security_token=token,
                pin=pin,
                tenant=tenant
            )

            resolved_user_id = user_id or await self._resolve_user_id_after_register()

            serialized = (
                self.credential.model_dump_json()
                if hasattr(self.credential, "model_dump_json")
                else self.credential.json()
            )

            save_credential(resolved_user_id, serialized)
            return resolved_user_id

        except (WrongTokenException, UsedTokenException) as e:
            await self._close_api_if_needed()
            raise RuntimeError(f"Rejestracja nie powiodła się: {e}") from e

    async def _resolve_user_id_after_register(self) -> str:
        """Próbuje zbudować stabilne user_id bez wymagania go od klienta."""
        try:
            accounts = await self.get_accounts()
            if accounts:
                login = getattr(accounts[0], "login", {}) or {}
                login_id = login.get("Id")
                email = login.get("Value")
                if login_id:
                    return str(login_id)
                if email:
                    return str(email).lower()
        except Exception:
            # Awaryjnie generujemy losowy identyfikator, żeby nie blokować rejestracji.
            pass

        return f"user-{uuid.uuid4()}"

    # =====================================
    # LOAD SAVED CREDENTIAL
    # =====================================
    async def load_user_credential(self, user_id: str):
        serialized = load_credential(user_id)
        if not serialized:
            raise RuntimeError("Brak zapisanych credentials")
        

        self.credential = RsaCredential.model_validate_json(serialized)
        self.api = IrisHebeApi(self.credential)
        self.current_account = None  # reset kontekstu konta

    # =====================================
    # ACCOUNTS
    # =====================================
    async def get_accounts(self):
        api = self._ensure_api()
        accounts = await api.get_accounts()
        if accounts and self.current_account is None:
            self.current_account = accounts[0]
        return accounts

    async def get_accounts_raw(self):
        accounts = await self.get_accounts()
        return [a.model_dump() for a in accounts]

    # =====================================
    # OCENY
    # =====================================
    async def get_grades(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_grades(
            rest_url=acc.unit.rest_url,
            unit_id=acc.unit.id,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )

    # =================================
    # ŚREDNIA OCEN
    # =================================
    async def get_grades_averages(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_grades_averages(
            rest_url=acc.unit.rest_url,
            unit_id=acc.unit.id,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )

    # ====================================
    # OCENY KOŃCOWOROCZNE I ŚRÓDROCZNE
    # ====================================
    async def get_grades_summary(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_grades_summary(
            rest_url=acc.unit.rest_url,
            unit_id=acc.unit.id,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )

    # =====================================
    # SPRAWDZIANY
    # =====================================
    async def get_exams(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_exams(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date(2025, 12, 8),
            date_to=date(2025, 12, 17),
        )
    
    # =====================================
    # SZCZĘŚLIWY NUMEREK (LUCKY NUMBER)
    # =====================================
    async def get_lucky_number(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_lucky_number(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            constituent_unit_id=acc.constituent_unit.id,
            day = date.today(),
        )
    
    # =====================================
    # PODSTAWOWA FREKWENCJA
    # =====================================
    async def get_attendance(self):
        """Pobiera podstawową frekwencję ucznia dla bieżącego okresu"""
        if self.current_account is None:
            await self.get_accounts()  # Upewnia się, że mamy konto ucznia

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_presence(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )


    # =====================================
    # FREKWENCJA DODATKOWA (usprawiedliwienia, dodatkowe nieobecności)
    # =====================================
    async def get_presence_extra(self, date_from=None, date_to=None):
        """Pobiera dodatkowe informacje o frekwencji ucznia w podanym przedziale dat"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        if date_from is None:
            date_from = date.today()
        if date_to is None:
            date_to = date_from + timedelta(days=7)

        return await api.get_presence_extra(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )


    # =====================================
    # SZCZEGÓŁY FREKWENCJI DODATKOWEJ
    # =====================================
    async def get_presence_extra_info(self, weak_ref_id, type_):
        """Pobiera szczegółowe informacje o wybranej pozycji frekwencji dodatkowej"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_presence_extra_info(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            weak_ref_id=weak_ref_id,
            type_=type_,
        )


    # =====================================
    # STATYSTYKI MIESIĘCZNE FREKWENCJI
    # =====================================
    async def get_presence_month_stats(self):
        """Pobiera statystyki frekwencji ucznia podsumowane miesięcznie"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_presence_month_stats(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )


    # =====================================
    # STATYSTYKI FREKWENCJI PER PRZEDMIOT
    # =====================================
    async def get_presence_subject_stats(self):
        """Pobiera statystyki frekwencji ucznia dla poszczególnych przedmiotów"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_presence_subject_stats(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )

    # =====================================
    # PLAN LEKCJI (HARMONOGRAM)
    # =====================================
    async def get_schedule(self, date_from=None, date_to=None):
        """Pobiera plan lekcji ucznia w wybranym przedziale dat"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        if date_from is None:
            date_from = date.today()
        if date_to is None:
            date_to = date_from + timedelta(days=7)

        return await api.get_schedule(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )


    # =====================================
    # PLAN LEKCJI DODATKOWY / ZMIANY
    # =====================================
    async def get_schedule_extra(self, date_from=None, date_to=None):
        """Pobiera zmiany w planie lekcji ucznia w wybranym przedziale dat"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        if date_from is None:
            date_from = date.today()
        if date_to is None:
            date_to = date_from + timedelta(days=7)

        return await api.get_schedule_extra(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )
    
    # ==============================
    # PLANOWANE LEKCJE
    # ==============================
    async def get_planned_lessons(self, date_from=None, date_to=None):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        if date_from is None:
            date_from = date.today()
        if date_to is None:
            date_to = date_from + timedelta(days=7)

        return await api.get_planned_lessons(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )

    # ==============================
    # NAUCZYCIELE
    # ==============================
    async def get_teachers(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_teachers(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            period_id=acc.periods[-1].id,
        )
    
    # ==============================
    # SZKOŁA - INFORMACJE
    # ==============================
    async def get_school_info(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_school_info(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
        )
    
    # ==============================
    # UWAGI / POCHWAŁY
    # ==============================
    async def get_notes(self):
        if self.current_account is None:
            await self.get_accounts()

        api = self._ensure_api()
        acc = self.current_account

        return await api.get_notes(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
        )

    # =====================================
    # PRZERWY W NAUCE / DNI WOLNE
    # =====================================
    async def get_vacations(self, date_from=None, date_to=None):
        """Pobiera listę wakacji/przerw ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_vacations(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )
    
    # =================================
    # ZADANIA DOMOWE
    # =================================
    async def get_homework(self, date_from=None, date_to=None):
        """Pobiera listę zadań domowych ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_homework(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
        )
    
    # =================================
    # ZEBRANIA
    # =================================
    async def get_meetings(self, date_from=None, date_to=None):
        """Pobiera listę zebrań ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_meetings(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            #date_to=date_to
        )
    
    # =================================
    # OGŁOSZENIA
    # =================================
    async def get_announcements(self):
        """Pobiera listę ogłoszeń ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_announcements(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
        )
    
    # =================================
    # POSIŁKI (MEALS)
    # =================================
    async def get_meals(self, date_from=None, date_to=None, full=bool):
        """Pobiera listę posiłków ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_meal_menu(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            date_from=date_from,
            date_to=date_to,
            full=full
        )
    
    # /---- WIADMOŚCI ----/ #    
    # ==============================
    # OTRZYMANE WIADOMOŚCI
    # ==============================
    async def get_received_messages(self, box: str):
        """Pobiera listę otrzymanych wiadomości ucznia"""
        if self.current_account is None:
            await self.get_accounts()

        acc = self.current_account
        api = self._ensure_api()

        return await api.get_received_messages(
            rest_url=acc.unit.rest_url,
            pupil_id=acc.pupil.id,
            box=box
        )
