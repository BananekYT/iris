from iris.credentials import RsaCredential
from iris.api import IrisHebeApi

from datetime import date
from iris._exceptions import WrongTokenException, UsedTokenException
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
        def _collect_sessions(obj, _visited=None):
            sessions = []
            if obj is None:
                return sessions
            if _visited is None:
                _visited = set()
            try:
                obj_id = id(obj)
            except Exception:
                return sessions
            if obj_id in _visited:
                return sessions
            _visited.add(obj_id)

            # direct match
            if isinstance(obj, aiohttp.ClientSession):
                sessions.append(obj)
                return sessions

            # iterate common collections
            if isinstance(obj, (list, tuple, set)):
                for item in obj:
                    sessions.extend(_collect_sessions(item, _visited))
                return sessions
            if isinstance(obj, dict):
                for k, v in obj.items():
                    sessions.extend(_collect_sessions(k, _visited))
                    sessions.extend(_collect_sessions(v, _visited))
                return sessions

            # inspect attributes of arbitrary objects
            for name in dir(obj):
                if name.startswith("__"):
                    continue
                try:
                    attr = getattr(obj, name)
                except Exception:
                    continue
                # skip callables and modules to avoid executing code
                if inspect.isroutine(attr) or inspect.ismodule(attr):
                    continue
                sessions.extend(_collect_sessions(attr, _visited))

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

    async def register(self, pin: str, token: str, tenant: str):
        # jeśli credential ma już ustawiony rest_url, uznajemy, że jest zarejestrowany
        if self.credential is not None and getattr(self.credential, "rest_url", None):
            return self.credential.rest_url

        api = self._ensure_api()
        try:
            # Rejestracja token+PIN
            await api.register_by_token(security_token=token, pin=pin, tenant=tenant)
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
        except WrongTokenException as e:
            # Przyjazny komunikat dla nieprawidłowego tokena
            await self._close_api_if_needed()
            raise RuntimeError("Nieprawidłowy token rejestracyjny (TOKEN). Sprawdź wartość w konfiguracji.") from e
        except UsedTokenException as e:
            # Token już był użyty — możliwe, że masz zapisane poświadczenia w credential.json
            await self._close_api_if_needed()
            msg = (
                "Token już był użyty. Jeśli wcześniej rejestrowałeś aplikację, sprawdź plik 'app/credential.json'\n"
                "i użyj zapisanych poświadczeń. Jeśli to nowa instalacja, poproś o nowy token od szkoły."
            )
            raise RuntimeError(msg) from e
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
        envelope = await api.login_by_login_password(
            login=login,
            password=password,
            symbol=symbol,
        )

        # pomocnicza funkcja: rekurencyjnie szuka kluczy zawierających 'session' lub 'token'
        def _find_token(obj, _visited=None):
            if _visited is None:
                _visited = set()
            try:
                oid = id(obj)
            except Exception:
                return None
            if oid in _visited:
                return None
            _visited.add(oid)

            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, str) and ("session" in k.lower() or "token" in k.lower()):
                        if isinstance(v, str) and len(v) > 5:
                            return v
                    res = _find_token(v, _visited)
                    if res:
                        return res
            elif isinstance(obj, (list, tuple)):
                for it in obj:
                    res = _find_token(it, _visited)
                    if res:
                        return res
            return None

        # spróbuj znaleźć token w surowej odpowiedzi
        session_token = _find_token(envelope)

        # spróbuj wyekstrahować obiekt konta (dict zawierający pola typowe dla Account)
        def _find_account_dict(obj, _visited=None):
            if _visited is None:
                _visited = set()
            try:
                oid = id(obj)
            except Exception:
                return None
            if oid in _visited:
                return None
            _visited.add(oid)

            if isinstance(obj, dict):
                # heurystyka: obecność klucza 'TopLevelPartition' lub 'Unit' sugeruje obiekt Account
                if any(k in obj for k in ("TopLevelPartition", "Unit", "Pupil")):
                    return obj
                for v in obj.values():
                    res = _find_account_dict(v, _visited)
                    if res:
                        return res
            elif isinstance(obj, (list, tuple)):
                for it in obj:
                    res = _find_account_dict(it, _visited)
                    if res:
                        return res
            return None

        account_data = _find_account_dict(envelope)
        account_obj = None
        try:
            if account_data is not None:
                from iris.models import Account

                account_obj = Account.model_validate(account_data)
                # dołącz token jeśli znaleziono
                if session_token:
                    setattr(account_obj, "session_token", session_token)
        except Exception:
            account_obj = None

        # jeśli nie znaleziono account_obj, spróbuj gdy envelope jest listą z jednym elementem
        if account_obj is None and isinstance(envelope, list) and envelope:
            try:
                from iris.models import Account

                possible = envelope[0]
                if isinstance(possible, dict):
                    account_obj = Account.model_validate(possible)
                    if session_token:
                        setattr(account_obj, "session_token", session_token)
            except Exception:
                account_obj = None

        # zapamiętujemy konto dla późniejszych wywołań (jeśli znaleziono)
        if account_obj is not None:
            self.current_account = account_obj

        return {
            "token": session_token,
            "student": account_obj.model_dump() if account_obj is not None else None,
            "raw": envelope,
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
            date_from=date(2025, 12, 8),
            date_to=date(2025, 12, 17)
        )      
        return exams