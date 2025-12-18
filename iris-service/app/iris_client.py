from iris.credentials import RsaCredential
from iris.api import IrisHebeApi
from .secure_credential import save_credential, load_credential

from datetime import date
from iris._exceptions import WrongTokenException, UsedTokenException
import asyncio
import aiohttp
import inspect
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
                raise RuntimeError(
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
    async def register(self, pin: str, token: str, tenant: str, user_id: str):
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

            serialized = (
                self.credential.model_dump_json()
                if hasattr(self.credential, "model_dump_json")
                else self.credential.json()
            )

            save_credential(user_id, serialized)

        except (WrongTokenException, UsedTokenException) as e:
            await self._close_api_if_needed()
            raise RuntimeError(f"Rejestracja nie powiodła się: {e}") from e

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
    # GRADES
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

    # =====================================
    # EXAMS
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
