#
# This file is the main entry point for the application.
# It has been updated to launch the new FastAPI application using Uvicorn.
# This ensures backward compatibility with deployment scripts that expect to run `python main.py`.
#
# For production, it's recommended to use a production-grade ASGI server like Uvicorn
# with multiple workers, managed by a process manager like Gunicorn.
#
# Example for production with Gunicorn:
# gunicorn -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:5000 app.main:app
#
# The ARQ worker for processing background jobs must be run in a separate process:
# arq app.services.task_queue.WorkerSettings
#

import uvicorn
from dotenv import load_dotenv
from loguru import logger

# Load environment variables from .env file before any other imports
load_dotenv()

from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402

# --- Main Execution ---
if __name__ == "__main__":
    # Setup logging to ensure consistent output format
    setup_logging()
    settings = get_settings()

    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    logger.info(
        "For production, run the app and worker separately using Gunicorn and ARQ CLI."
    )
    logger.warning(
        "This development server runs the app only. Remember to start the ARQ worker."
    )

    # Run the FastAPI app using Uvicorn
    # The host is set to 0.0.0.0 to be accessible from the network.
    # The port is set to 5000 to match the original Flask application's port.
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=5000,
        reload=True,  # Enable auto-reload for development
        log_level="info",
    )
