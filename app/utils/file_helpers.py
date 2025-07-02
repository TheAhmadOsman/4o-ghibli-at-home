import asyncio
import os
from pathlib import Path

import aiofiles
from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings

settings = get_settings()


async def validate_image(file: UploadFile) -> Image.Image:
    """
    Validates an uploaded image file.

    Checks for:
    - Allowed file extension.
    - Valid image content by trying to open it.

    Args:
        file: The uploaded file from FastAPI.

    Returns:
        A PIL Image object if valid.

    Raises:
        HTTPException: If the file is invalid, with a 400 status code.
    """
    # Check file extension
    ext = file.filename.split(".")[-1].lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed extensions are: {', '.join(settings.ALLOWED_EXTENSIONS)}",
        )

    # Read file content into memory to validate it
    # This is necessary because Pillow needs a file-like object that supports seek
    contents = await file.read()
    # Reset the file pointer in case it's used again (though we use the in-memory content)
    await file.seek(0)

    try:
        # Use a BytesIO stream to open the image from memory
        from io import BytesIO

        image = Image.open(BytesIO(contents)).convert("RGB")
        return image
    except UnidentifiedImageError:
        raise HTTPException(
            status_code=400,
            detail="Cannot identify image file. The file may be corrupt.",
        )
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"An error occurred while processing the image: {e}"
        )


async def save_generated_image(job_id: str, image: Image.Image) -> Path:
    """
    Saves a generated PIL Image to the results folder.

    Args:
        job_id: The unique identifier for the job.
        image: The PIL Image object to save.

    Returns:
        The path to the saved image file.
    """
    results_dir = Path(settings.RESULTS_FOLDER)
    results_dir.mkdir(exist_ok=True)
    file_path = results_dir / f"{job_id}.png"

    # Saving the image is a blocking I/O operation.
    # Run it in a separate thread to avoid blocking the event loop.
    await asyncio.to_thread(image.save, file_path, "PNG")

    return file_path


async def cleanup_expired_results(ttl: int):
    """
    Deletes generated image files that are older than the specified TTL.

    Args:
        ttl: The time-to-live in seconds.
    """
    results_dir = Path(settings.RESULTS_FOLDER)
    if not results_dir.exists():
        return 0

    now = asyncio.to_thread(os.path.getmtime, results_dir)
    cleaned_count = 0
    for file_path in results_dir.glob("*.png"):
        try:
            if await asyncio.to_thread(file_path.is_file):
                file_mod_time = await asyncio.to_thread(os.path.getmtime, file_path)
                if (now - file_mod_time) > ttl:
                    await aiofiles.os.remove(file_path)
                    cleaned_count += 1
        except FileNotFoundError:
            # File might have been deleted by another process
            continue
    return cleaned_count


def get_result_path(job_id: str) -> Path:
    """
    Gets the expected path for a result file.

    Args:
        job_id: The job ID.

    Returns:
        The Path object for the result file.
    """
    return Path(settings.RESULTS_FOLDER) / f"{job_id}.png"
