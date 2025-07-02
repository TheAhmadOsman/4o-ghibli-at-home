import time
from typing import Any, Dict, Optional

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus
from loguru import logger
from PIL import Image

from app.core.config import get_settings
from app.models.job import JobResult
from app.services.ml_model import get_ml_model
from app.utils.file_helpers import save_generated_image

settings = get_settings()


async def generate_image_task(ctx, job_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    ARQ task to generate an image.
    This function is executed by the ARQ worker.
    """
    job_id = ctx["job_id"]
    ml_model = get_ml_model()
    start_time = time.time()

    log_params = {
        k: v for k, v in job_params.items() if k not in ["image", "generator"]
    }
    logger.info(f"ARQ job {job_id} started with params: {log_params}")

    try:
        # The 'image' parameter is a PIL Image, which is not directly serializable by ARQ.
        # It needs to be handled appropriately before being passed to the task.
        # For this implementation, we assume the image has been pre-processed
        # and the relevant data is in job_params.

        # Re-create the PIL image if it was serialized
        if "image_bytes" in job_params:
            from io import BytesIO

            job_params["image"] = Image.open(
                BytesIO(job_params.pop("image_bytes"))
            ).convert("RGB")

        generated_image = await ml_model.generate_image(job_params)
        result_path = await save_generated_image(job_id, generated_image)

        end_time = time.time()
        duration = end_time - start_time
        logger.info(f"ARQ job {job_id} completed in {duration:.2f} seconds.")

        return {
            "status": "completed",
            "result_path": str(result_path),
            "completion_time": end_time,
        }
    except Exception as e:
        logger.exception(f"ARQ job {job_id} failed: {e}")
        return {"status": "failed", "error": str(e)}


class TaskQueueService:
    def __init__(self):
        self.redis_pool = None

    async def startup(self):
        logger.info("Initializing ARQ task queue...")
        self.redis_pool = await create_pool(
            RedisSettings(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                database=settings.REDIS_DB,
            )
        )
        logger.info("ARQ task queue initialized.")

    async def shutdown(self):
        if self.redis_pool:
            await self.redis_pool.close()
            logger.info("ARQ task queue shut down.")

    async def submit_job(self, job_id: str, params: Dict[str, Any]) -> Job:
        # PIL Images are not serializable. Convert to bytes.
        if "image" in params and isinstance(params["image"], Image.Image):
            from io import BytesIO

            buffer = BytesIO()
            params["image"].save(buffer, format="PNG")
            params["image_bytes"] = buffer.getvalue()
            del params["image"]

        job = await self.redis_pool.enqueue_job(
            "generate_image_task", params, _job_id=job_id
        )
        return job

    async def get_job_status(self, job_id: str) -> Optional[JobResult]:
        job = Job(job_id, self.redis_pool)
        info = await job.info()

        if not info:
            return None

        status = info.status
        result = None
        queue_position = None

        if status == JobStatus.complete:
            result = await job.result()
        elif status == JobStatus.queued:
            # This is an approximation. ARQ doesn't provide a direct queue position.
            # We can get the list of queued jobs and find our position.
            queued_jobs = await self.redis_pool.queued_jobs()
            for i, q_job in enumerate(queued_jobs):
                if q_job.job_id == job_id:
                    queue_position = i + 1
                    break

        return JobResult(
            job_id=job_id,
            status=status,
            queue_position=queue_position,
            result=result,
            enqueue_time=info.enqueue_time,
            start_time=info.start_time,
            finish_time=info.finish_time,
        )


# Singleton instance
task_queue = TaskQueueService()


def get_task_queue() -> TaskQueueService:
    return task_queue


# ARQ Worker settings
class WorkerSettings:
    functions = [generate_image_task]
    redis_settings = RedisSettings(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT, database=settings.REDIS_DB
    )
    max_jobs = settings.MAX_CONCURRENT_JOBS
    job_timeout = settings.JOB_TIMEOUT
