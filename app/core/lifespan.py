from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.services.ml_model import get_ml_model
from app.services.task_queue import get_task_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Handles startup and shutdown events for the FastAPI application.
    - On startup: Loads the ML model and initializes the task queue.
    - On shutdown: Cleans up resources.
    """
    logger.info("--- Application Startup ---")

    # Load the ML model
    ml_model = get_ml_model()
    try:
        await ml_model.load_model()
        app.state.ml_model = ml_model
    except Exception as e:
        logger.critical(f"Failed to load ML model: {e}")
        # Depending on the desired behavior, you might want to exit the app
        # if the model fails to load. For now, we log a critical error.
        # raise e # Re-raising would stop the app from starting.

    # Initialize the task queue service
    task_queue = get_task_queue()
    try:
        await task_queue.startup()
        app.state.task_queue = task_queue
    except Exception as e:
        logger.critical(f"Failed to initialize task queue: {e}")
        # Similar to the model, you might want to handle this failure explicitly.

    yield

    logger.info("--- Application Shutdown ---")

    # Shutdown the task queue service
    if hasattr(app.state, "task_queue") and app.state.task_queue:
        await app.state.task_queue.shutdown()

    # Add any other resource cleanup here (e.g., database connections)
    logger.info("Cleanup complete. Exiting.")
