class GDFLAPIError(Exception):
    """Raised when the GlobalDataFeeds API returns a non-success response."""

    def __init__(self, message: str, status_code: int | None = None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
