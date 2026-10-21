# core/exceptions.py
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, message: str = "Something went wrong", errors: list | None = None):
        self.status_code = status_code
        self.message = message
        self.errors = errors or []
        super().__init__(message)

async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.message, "errors": exc.errors, "data": None},
    )


async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"success": False, "message": "Validation failed", "errors": exc.errors(), "data": None},
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": str(exc.detail), "errors": [], "data": None},
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    print("Unhandled error:", exc)  # we'll replace with real logging later
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "Internal server error", "errors": [], "data": None},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)