import os
import io
import torch
from celery import Celery
from celery.signals import worker_process_init
from loguru import logger
from PIL import Image
from dotenv import load_dotenv

# --- Dotenv Configuration ---
load_dotenv()

# --- Celery Configuration ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery(
    "tasks",
    broker=REDIS_URL,
    backend=REDIS_URL
)

# --- Model Initialization ---
# The model is a global variable for the worker process.
# It will be initialized by the worker_process_init signal handler.
pipe = None

@worker_process_init.connect
def initialize_model(**kwargs):
    """
    This function is called by the Celery worker process when it starts.
    It loads the model into memory, ensuring it's ready before any tasks are run.
    This code is NOT executed by the Gunicorn web server process.
    """
    global pipe
    logger.info("WORKER PROCESS INIT: Initializing model...")
    try:
        # Use environment variable for device, with auto-detection as fallback
        _default_device = "cuda" if torch.cuda.is_available() else "cpu"
        DEVICE = os.getenv("PYTORCH_DEVICE", _default_device)
        TORCH_DTYPE = (
            torch.bfloat16 if DEVICE == "cuda" else torch.float32
        )

        if not torch.cuda.is_available():
            logger.warning(
                "CUDA not available. Running on CPU, which will be extremely slow."
            )

        from dfloat11 import DFloat11Model
        from diffusers import FluxKontextPipeline

        hf_token = os.getenv("HUGGING_FACE_HUB_TOKEN")
        
        pipe = FluxKontextPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-Kontext-dev", 
            torch_dtype=TORCH_DTYPE, 
            use_auth_token=hf_token
        )
        DFloat11Model.from_pretrained(
            "DFloat11/FLUX.1-Kontext-dev-DF11",
            device="cpu",
            bfloat16_model=pipe.transformer,
            use_auth_token=hf_token
        )

        if DEVICE == "cuda":
            pipe.enable_model_cpu_offload()
        else:
            pipe.to(DEVICE)

        logger.info(f"Model initialized successfully on device '{DEVICE}'.")

    except ImportError as e:
        logger.critical(f"A required library is not installed. {e}")
        raise
    except Exception as e:
        logger.critical(f"Could not initialize the model for the Celery worker. Error: {e}")
        raise

# --- Celery Task Definition ---
@celery_app.task(bind=True)
def generate_image_task(self, pipe_kwargs, result_folder):
    """
    Celery task to generate an image using the diffusion pipeline.
    """
    global pipe
    if pipe is None:
        # This is a fallback in case the worker signal didn't fire,
        # or for certain testing scenarios.
        logger.warning("Pipe not initialized, attempting to initialize now...")
        initialize_model()

    job_id = self.request.id
    
    # Recreate the PIL Image object from the bytes that were passed.
    image_bytes = pipe_kwargs.pop("image_bytes")
    image_info = pipe_kwargs.pop("image_info")
    input_image = Image.open(io.BytesIO(image_bytes))
    pipe_kwargs["image"] = input_image

    log_params = {
        k: v for k, v in pipe_kwargs.items() if k != "image"
    }
    log_params["seed"] = pipe_kwargs["seed_value"]
    logger.info(f"Processing job {job_id} with params: {log_params}")
    
    device = os.getenv("PYTORCH_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
    pipe_kwargs["generator"] = torch.Generator(device=device).manual_seed(pipe_kwargs["seed_value"])
    pipe_kwargs.pop("seed_value")

    try:
        processed_image = pipe(**pipe_kwargs).images[0]
        os.makedirs(result_folder, exist_ok=True)
        result_filename = f"{job_id}.png"
        result_path = os.path.join(result_folder, result_filename)
        processed_image.save(result_path, "PNG")

        logger.info(f"Job {job_id} completed successfully.")
        return {"status": "completed", "result_path": result_path}

    except torch.cuda.OutOfMemoryError as e:
        error_message = "Processing failed due to insufficient GPU memory. Try a smaller image size."
        logger.error(f"Job {job_id} failed: {error_message} - {e}")
        raise Exception(error_message) from e
    except Exception as e:
        logger.exception(f"An unexpected error occurred in worker for job {job_id}: {e}")
        raise