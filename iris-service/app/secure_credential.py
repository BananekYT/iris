import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

from .errors import CredentialKeyInvalidError, CredentialKeyMissingError

# katalog główny projektu
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # app/ -> ../
CREDENTIAL_DIR = PROJECT_ROOT / "credentials"
CREDENTIAL_DIR.mkdir(exist_ok=True)


def _harden_permissions(path: Path, mode: int) -> None:
    """Best-effort: ustaw restrykcyjne prawa dostępu (Unix)."""
    try:
        path.chmod(mode)
    except OSError:
        # Na niektórych platformach (np. Windows) chmod bywa ograniczone.
        pass


def _get_fernet() -> Fernet:
    key = os.environ.get("CREDENTIAL_KEY")
    if not key:
        raise CredentialKeyMissingError(
            "Brak zmiennej środowiskowej CREDENTIAL_KEY"
        )

    try:
        key_bytes = key.encode("utf-8")
        Fernet(key_bytes)  # walidacja formatu
    except Exception as exc:  # noqa: BLE001 - chcemy mapować dowolny błąd do AppError
        raise CredentialKeyInvalidError() from exc

    return Fernet(key_bytes)


def _filename_for_user_id(user_id: str) -> str:
    """
    Zwraca nieodwracalny identyfikator pliku na bazie SHA-256, aby nie ujawniać user_id.
    """
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _credential_path(user_id: str) -> Path:
    return CREDENTIAL_DIR / f"{_filename_for_user_id(user_id)}.bin"


def save_credential(user_id: str, serialized_json: str) -> None:
    f = _get_fernet()
    encrypted = f.encrypt(serialized_json.encode("utf-8"))

    CREDENTIAL_DIR.mkdir(exist_ok=True)
    _harden_permissions(CREDENTIAL_DIR, 0o700)

    file_path = _credential_path(user_id)
    tmp_path = file_path.with_suffix(".tmp")

    tmp_path.write_bytes(encrypted)
    _harden_permissions(tmp_path, 0o600)
    tmp_path.replace(file_path)
    _harden_permissions(file_path, 0o600)


def load_credential(user_id: str) -> str | None:
    file_path = _credential_path(user_id)
    if not file_path.exists():
        return None

    f = _get_fernet()
    encrypted = file_path.read_bytes()
    return f.decrypt(encrypted).decode("utf-8")
