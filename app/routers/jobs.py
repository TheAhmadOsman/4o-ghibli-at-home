import uuid

import torch
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from loguru import logger

from app.core.config import get_settings
from app.models.job import JobResult, JobSubmissionResponse
from app.services.task_queue import TaskQueueService, get_task_queue
from app.utils.file_helpers import get_result_path, validate_image

router = APIRouter()
settings = get_settings()


def parse_and_validate_params(
    image: UploadFile,
    prompt: str = Form(""),
    width: int = Form(settings.DEFAULT_WIDTH),
    height: int = Form(settings.DEFAULT_HEIGHT),
    num_inference_steps: int = Form(settings.DEFAULT_STEPS),
    guidance_scale: float = Form(settings.DEFAULT_GUIDANCE_SCALE),
    true_cfg_scale: float = Form(settings.DEFAULT_TRUE_CFG_SCALE),
    seed: int = Form(None),
    prompt_2: str = Form(None),
    negative_prompt: str = Form(None),
    negative_prompt_2: str = Form(None),
):
    """
    Parses and validates form parameters for the image generation job.
    This function is used as a dependency in the endpoint.
    """
    try:
        validated_image = validate_image(image)

        if seed is None:
            seed = torch.randint(0, 2**32 - 1, (1,)).item()

        params = {
            "image": validated_image,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "true_cfg_scale": true_cfg_scale,
            "seed_value": seed,
            "max_sequence_length": 512,
            "num_images_per_prompt": 1,
        }

        # Add optional prompts if they are provided
        if prompt_2:
            params["prompt_2"] = prompt_2
        if negative_prompt:
            params["negative_prompt"] = negative_prompt
        if negative_prompt_2:
            params["negative_prompt_2"] = negative_prompt_2

        return params
    except HTTPException as e:
        # Re-raise HTTPExceptions from validation
        raise e
    except Exception as e:
        logger.warning(f"Invalid parameter provided: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Invalid parameter type provided. Details: {e}",
        )


@router.post(
    "/process-image",
    response_model=JobSubmissionResponse,
    status_code=202,
    summary="Submit an image generation job",
)
async def submit_job(
    params: dict = Depends(parse_and_validate_params),
    task_queue: TaskQueueService = Depends(get_task_queue),
):
    """
    Accepts an image and generation parameters, and queues a job.
    """
    job_id = str(uuid.uuid4())
    try:
        await task_queue.submit_job(job_id, params)
        logger.info(f"Job {job_id} accepted and queued.")
        return JobSubmissionResponse(
            message="Request accepted and queued.",
            job_id=job_id,
            status_url=f"/status/{job_id}",
            result_url=f"/result/{job_id}",
        )
    except Exception as e:
        logger.exception(f"Failed to submit job {job_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Server is currently busy or unable to queue new jobs. Please try again later.",
        )


@router.get(
    "/status/{job_id}",
    response_model=JobResult,
    summary="Get job status",
)
async def get_job_status(
    job_id: str, task_queue: TaskQueueService = Depends(get_task_queue)
):
    """
    Provides the status of a specific job, including queue position if applicable.
    """
    status = await task_queue.get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job ID not found.")
    return status


@router.get(
    "/result/{job_id}",
    response_class=FileResponse,
    summary="Get job result",
    responses={
        200: {
            "content": {"image/png": {}},
            "description": "The generated image.",
        },
        202: {"description": "Job is not yet complete."},
        404: {"description": "Job ID not found."},
        500: {"description": "Job failed or result is missing."},
    },
)
async def get_job_result(
    job_id: str, task_queue: TaskQueueService = Depends(get_task_queue)
):
    """
    Serves the generated image if the job is complete.
    """
    status = await task_queue.get_job_status(job_id)

    if not status:
        raise HTTPException(status_code=404, detail="Job ID not found.")

    if status.status == "completed":
        result_path = get_result_path(job_id)
        if result_path.exists():
            return FileResponse(result_path, media_type="image/png")
        else:
            logger.error(
                f"Result file for completed job {job_id} not found at {result_path}"
            )
            raise HTTPException(status_code=500, detail="Result file is missing.")
    elif status.status == "failed":
        error_detail = (
            status.result.get("error", "An unknown error occurred.")
            if status.result
            else "An unknown error occurred."
        )
        raise HTTPException(status_code=500, detail=error_detail)
    else:
        # For "queued" or "processing" status
        raise HTTPException(
            status_code=202,
            detail=f"Job is not yet complete. Current status: {status.status}",
        )
