import os
from cryptography.fernet import Fernet
from pathlib import Path
import base64
from .errors import CredentialKeyMissingError

# katalog główny projektu
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # app/ -> ../
CREDENTIAL_DIR = PROJECT_ROOT / "credentials"
CREDENTIAL_DIR.mkdir(exist_ok=True)

def _get_fernet() -> Fernet:
    key = os.environ.get("CREDENTIAL_KEY")
    from .errors import CredentialKeyMissingError

    if not key:
        raise CredentialKeyMissingError(
            "Brak zmiennej środowiskowej CREDENTIAL_KEY"
        )
    return Fernet(key.encode())

def _encode_user_id(user_id: str) -> str:
    """Zamień login/email na bezpieczną nazwę pliku"""
    return base64.urlsafe_b64encode(user_id.encode()).decode()

def _decode_user_id(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded.encode()).decode()

def save_credential(user_id: str, serialized_json: str):
    f = _get_fernet()
    encrypted = f.encrypt(serialized_json.encode())
    file_path = CREDENTIAL_DIR / f"{_encode_user_id(user_id)}.json"
    file_path.write_bytes(encrypted)

def load_credential(user_id: str) -> str | None:
    file_path = CREDENTIAL_DIR / f"{_encode_user_id(user_id)}.json"
    if not file_path.exists():
        return None
    f = _get_fernet()
    encrypted = file_path.read_bytes()
    return f.decrypt(encrypted).decode()
