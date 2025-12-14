from iris.credentials import RsaCredential
from iris.api import IrisHebeApi

from .config import TOKEN, PIN, TENANT
from datetime import date
import asyncio
import aiohttp
import inspect
from pathlib import Path

CREDENTIAL_FILE = Path(__file__).parent / "credential.json"

class IrisClient:
    def __init__(self):
        # Nie twórz credential/api od razu — twórz leniwie, by uniknąć otwartych sesji przy błędach
        self.device_name = "Android"
        self.device_model = "SM-A525F"
        self.credential = None
        self.api = None
        self.current_account = None  # przechowuje ostatnio używane konto

        # Spróbuj wczytać zapisany credential zgodnie z dokumentacją
        if CREDENTIAL_FILE.exists():
            try:
                data = CREDENTIAL_FILE.read_text(encoding="utf-8")
                # użyj model_validate_json jeśli dostępne
                if hasattr(RsaCredential, "model_validate_json"):
                    self.credential = RsaCredential.model_validate_json(data)
                else:
                    # fallback na pydantic v1 metoda
                    self.credential = RsaCredential.parse_raw(data)
                print("Loaded existing credential from", CREDENTIAL_FILE)
            except Exception as e:
                print("Warning: failed to load credential file:", repr(e))
                self.credential = None

    def _ensure_api(self):
        # utwórz credential i api dopiero przy pierwszym użyciu
        if self.api is None:
            if self.credential is None:
                self.credential = RsaCredential.create_new(self.device_name, self.device_model)
            self.api = IrisHebeApi(self.credential)
        return self.api

    async def _close_api_if_needed(self):
        # jeśli api ma metodę close, wywołaj ją (sync/async)
        if self.api is not None:
            close_fn = getattr(self.api, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res
        # dodatkowo przeszukaj atrybuty api/credential i zamknij znalezione ClientSession
        def _collect_sessions(obj):
            sessions = []
            if obj is None:
                return sessions
            for name in dir(obj):
                try:
                    attr = getattr(obj, name)
                except Exception:
                    continue
                if isinstance(attr, aiohttp.ClientSession):
                    sessions.append(attr)
            return sessions

        # zamknij sesje znalezione w api i credential
        for sess in _collect_sessions(self.api) + _collect_sessions(self.credential):
            try:
                res = sess.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                try:
                    # fallback: użyj await sess.close() jeśli callable
                    if inspect.iscoroutinefunction(sess.close):
                        await sess.close()
                except Exception:
                    pass

        # usuń referencje
        self.api = None
        # credential też sprzątaj jeśli ma close
        if self.credential is not None:
            close_cred = getattr(self.credential, "close", None)
            if callable(close_cred):
                res = close_cred()
                if asyncio.iscoroutine(res):
                    await res
            # nie usuwamy credential tutaj - pozostawiamy w pamięci (możemy wczytać ponownie)
            # jeśli chcemy całkowicie usunąć credential z pamięci, odkomentuj poniższą linię:
            # self.credential = None

    async def close(self):
        # jawne zamknięcie zasobów
        await self._close_api_if_needed()

    async def register(self):
        api = self._ensure_api()
        try:
            # Rejestracja token+PIN
            await api.register_by_token(security_token=TOKEN, pin=PIN, tenant=TENANT)
            # po udanej rejestracji zapisz credential do pliku zgodnie z dokumentacją
            try:
                if hasattr(self.credential, "model_dump_json"):
                    serialized = self.credential.model_dump_json()
                else:
                    # fallback dla starszych wersji pydantic
                    serialized = self.credential.json()
                CREDENTIAL_FILE.write_text(serialized, encoding="utf-8")
                print("Saved credential to", CREDENTIAL_FILE)
            except Exception as e:
                print("Warning: failed to save credential:", repr(e))
        except Exception as e:
            # Spróbuj alternatywnego formatu tokenu przed ostatecznym błędem
            try:
                alt_token = TOKEN.upper()
                if alt_token != TOKEN:
                    await api.register_by_token(security_token=alt_token, pin=PIN, tenant=TENANT)
                    try:
                        if hasattr(self.credential, "model_dump_json"):
                            serialized = self.credential.model_dump_json()
                        else:
                            serialized = self.credential.json()
                        CREDENTIAL_FILE.write_text(serialized, encoding="utf-8")
                        print("Saved credential to", CREDENTIAL_FILE)
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            # przy błędzie zamknij sesję i credential, żeby nie zostawiać "Unclosed client session"
            await self._close_api_if_needed()
            raise

    async def get_accounts(self):
        api = self._ensure_api()
        return await api.get_accounts()

    async def login(self, login: str, password: str, symbol: str):
        await self.register()
        api = self._ensure_api()
        account = await api.login_by_login_password(
            login=login,
            password=password,
            symbol=symbol
        )
        # zapamiętujemy konto dla późniejszych wywołań
        self.current_account = account
        return {
            "token": account.session_token,
            "student": account.student_info
        }
    
    async def _ensure_account_for_token(self, session_token: str):
        # spróbuj użyć zapamiętanego konta
        if self.current_account and getattr(self.current_account, "session_token", None) == session_token:
            return self.current_account
        # w przeciwnym razie pobierz konta i znajdź pasujące
        accounts = await self.get_accounts()
        for acc in accounts:
            if getattr(acc, "session_token", None) == session_token:
                self.current_account = acc
                return acc
        raise Exception("Account not found for given session_token")
    
    async def get_grades(self, session_token: str):
        api = self._ensure_api()
        await api.set_session_token(session_token)
        account = await self._ensure_account_for_token(session_token)
        grades = await api.get_grades(
            rest_url=account.unit.rest_url,
            unit_id=account.unit.id,
            pupil_id=account.pupil.id,
            period_id=account.periods[-1].id,
        )
        return grades
    
    async def get_exams(self, session_token: str):
        api = self._ensure_api()
        await api.set_session_token(session_token)
        account = await self._ensure_account_for_token(session_token)
        exams = await api.get_exams(
            rest_url=account.unit.rest_url,
            pupil_id=account.pupil.id,
            date_from=date(2020, 9, 1),
            date_to=date(2020, 9, 7)
        )      
        return exams