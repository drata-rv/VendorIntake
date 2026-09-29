class ApiFail(Exception):
    def __init__(self, status: int, code: str, message: str, field_errors: dict | None = None,
                 submission_id: str | None = None, headers: dict | None = None, extra: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.field_errors = field_errors or {}
        self.submission_id = submission_id
        self.headers = headers or {}
        self.extra = extra or {}

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message, "fieldErrors": self.field_errors,
               "submissionId": self.submission_id}
        err.update(self.extra)
        return {"error": err}
