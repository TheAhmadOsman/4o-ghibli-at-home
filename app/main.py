from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.lifespan import lifespan
from app.core.logging import setup_logging
from app.routers import jobs, profiles

# --- App Initialization ---
setup_logging()
settings = get_settings()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    # Disable the default exception handlers to allow for custom ones
    exception_handlers={},
)

# --- API Routers ---
logger.info("Attaching API routers...")
app.include_router(profiles.router, prefix="/api", tags=["Profiles"])
# The jobs router is mounted at the root to match the original Flask app's endpoints
app.include_router(jobs.router, tags=["Jobs"])


# --- Custom Error Handlers ---
# This section ensures that the FastAPI error responses are identical to the old Flask app,
# which is crucial for frontend compatibility.


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handles exceptions raised by FastAPI/Starlette, like 404 Not Found."""
    status_code = exc.status_code
    error_messages = {
        404: {
            "error": "Not Found",
            "message": "The requested URL was not found on the server.",
        },
        405: {
            "error": "Method Not Allowed",
            "message": "The method is not allowed for the requested URL.",
        },
    }
    content = error_messages.get(
        status_code, {"error": exc.detail, "message": "An HTTP error occurred."}
    )
    return JSONResponse(content=content, status_code=status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handles Pydantic validation errors, like invalid data types."""
    # For file uploads that are too large, Content-Length is checked by the server before validation.
    # We check for the specific 413 error case here.
    if (
        "Content-Length" in request.headers
        and int(request.headers["Content-Length"])
        > settings.MAX_UPLOAD_MB * 1024 * 1024
    ):
        return JSONResponse(
            status_code=413,
            content={
                "error": "Payload Too Large",
                "message": f"File upload is too large. Maximum size is {settings.MAX_UPLOAD_MB}MB.",
            },
        )
    # For other validation errors, return a generic 400.
    return JSONResponse(
        status_code=400,
        content={"error": "Bad Request", "message": str(exc)},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Handles any other unexpected server errors (500)."""
    logger.exception(f"An unhandled exception occurred: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": "An unexpected error occurred on the server.",
        },
    )


# --- Static Files ---
# This must be mounted last, after all routes and handlers are defined.
app.mount("/", StaticFiles(directory="static", html=True), name="static")

logger.info("Application setup complete.")
