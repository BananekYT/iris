# app/errors.py


class AppError(Exception):
    """Bazowa klasa dla własnych błędów API"""

    code: str = "UNKNOWN_ERROR"
    status_code: int = 500

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.__name__
        super().__init__(self.message)


class CredentialKeyMissingError(AppError):
    """Brak zmiennej środowiskowej CREDENTIAL_KEY"""

    code = "CREDENTIAL_KEY_MISSING"
    status_code = 500

    def __init__(self, message: str | None = None):
        super().__init__(message or "Brak zmiennej środowiskowej CREDENTIAL_KEY")


class CredentialKeyInvalidError(AppError):
    """Niepoprawny format klucza CREDENTIAL_KEY"""

    code = "CREDENTIAL_KEY_INVALID"
    status_code = 500

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "Niepoprawny format CREDENTIAL_KEY (wymagany klucz Fernet urlsafe-base64)."
        )


class CredentialNotFoundError(AppError):
    """Brak pliku credential dla użytkownika"""

    code = "CREDENTIAL_NOT_FOUND"
    status_code = 404

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "Brak credential. Najpierw wywołaj register() "
            "lub load_user_credential(user_id)"
        )


class WrongTokenError(AppError):
    """Nieprawidłowy token"""

    code = "WRONG_TOKEN"
    status_code = 401

    def __init__(self, message: str | None = None):
        super().__init__(message or "Nieprawidłowy token")


class JwtSecretMissingError(AppError):
    """Brak skonfigurowanego JWT_SECRET w środowisku produkcyjnym"""

    code = "JWT_SECRET_MISSING"
    status_code = 500

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "Brak zmiennej JWT_SECRET w środowisku produkcyjnym."
        )
