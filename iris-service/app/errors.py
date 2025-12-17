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


class CredentialNotFoundError(AppError):
    """Brak pliku credential dla użytkownika"""
    code = "CREDENTIAL_NOT_FOUND"
    status_code = 404
    def __init__(self, message: str | None = None):
        super().__init__(message or "Nie znaleziono credentials dla podanego ID")


class WrongTokenError(AppError):
    """Nieprawidłowy token sesji"""
    code = "WRONG_TOKEN"
    status_code = 401
    def __init__(self, message: str | None = None):
        super().__init__(message or "Nieprawidłowy token sesji")