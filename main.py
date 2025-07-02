#
# For production environments, it's highly recommended to use a proper WSGI server
# like Gunicorn instead of Flask's built-in development server.
# Example: gunicorn --workers 1 --threads 4 --timeout 600 -b 0.0.0.0:5000 app:app
#
# NOTE: The `--workers 1` is crucial because the model and the job queue are in-memory
# and not designed to be shared across multiple processes. For multi-worker scalability,
# a more robust task queue like Celery with Redis or RabbitMQ would be required.

import logging
import os
import sys
import threading
import time
import uuid
from collections import deque

import torch
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from loguru import logger
from PIL import Image, UnidentifiedImageError

# --- Dotenv Configuration ---
load_dotenv()


# --- Logging Configuration (Loguru) ---
# Remove default Flask logger and use Loguru for consistent, formatted logging
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)


# Intercept standard logging to capture logs from other libraries (like Gunicorn)
class InterceptHandler(logging.Handler):
    def emit(self, record):
        # --- START CHANGE ---
        # Filter out successful (INFO level) status polls to avoid cluttering the logs.
        # We check the message content and the log level. Errors on this endpoint will still be logged.
        if "GET /status/" in record.getMessage() and record.levelno == logging.INFO:
            return
        # --- END CHANGE ---

        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


# --- Application Configuration ---
class Config:
    # Maximum size of the job queue. Prevents server overload.
    MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", 10))
    # Maximum allowed upload size (e.g., 10 MB). Prevents DoS from large uploads.
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", 10))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
    # Allowed image upload extensions.
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
    # How long to keep job results in memory (in seconds)
    JOB_RESULT_TTL = int(os.getenv("JOB_RESULT_TTL", 600))  # 10 minutes
    # How often the cleanup worker runs (in seconds)
    CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 300))  # 5 minutes
    # Folder to store generated images
    RESULTS_FOLDER = os.getenv("RESULTS_FOLDER", "generated_images")
    # Default generation parameters
    DEFAULT_WIDTH = 1024
    DEFAULT_HEIGHT = 1024
    DEFAULT_STEPS = 28
    DEFAULT_GUIDANCE_SCALE = 2.5
    DEFAULT_TRUE_CFG_SCALE = 1.5


# --- Model Initialization ---
# This is a blocking, long-running operation, so it's done once at startup.
logger.info("Initializing model... This may take a few minutes.")
pipe = None
try:
    # Use environment variable for device, with auto-detection as fallback
    _default_device = "cuda" if torch.cuda.is_available() else "cpu"
    DEVICE = os.getenv("PYTORCH_DEVICE", _default_device)
    TORCH_DTYPE = (
        torch.bfloat16 if DEVICE == "cuda" else torch.float32
    )  # bfloat16 not supported on CPU for all ops

    if not torch.cuda.is_available():
        logger.warning(
            "CUDA not available. Running on CPU, which will be extremely slow."
        )

    from dfloat11 import DFloat11Model
    from diffusers import FluxKontextPipeline

    pipe = FluxKontextPipeline.from_pretrained(
        "black-forest-labs/FLUX.1-Kontext-dev", torch_dtype=TORCH_DTYPE
    )
    DFloat11Model.from_pretrained(
        "DFloat11/FLUX.1-Kontext-dev-DF11",
        device="cpu",  # DFloat11 specific, may need CPU
        bfloat16_model=pipe.transformer,
    )

    if DEVICE == "cuda":
        # Offloading is essential for consumer GPUs with limited VRAM
        pipe.enable_model_cpu_offload()
    else:
        pipe.to(DEVICE)

    logger.info(f"Model initialized successfully on device '{DEVICE}'.")

    # --- Create results directory ---
    try:
        os.makedirs(Config.RESULTS_FOLDER, exist_ok=True)
        logger.info(f"Results will be saved in '{Config.RESULTS_FOLDER}' directory.")
    except OSError as e:
        logger.critical(f"Could not create results directory. Error: {e}")
        exit(1)

except ImportError as e:
    logger.critical(f"A required library is not installed. {e}")
    exit(1)
except Exception as e:
    # This could be a huggingface connection error, file not found, etc.
    logger.critical(f"Could not initialize the model. Error: {e}")
    # The app is not functional without the model, so we exit.
    exit(1)


# --- Asynchronous Task Queue Setup ---
# This simple in-memory queue is for a single-worker setup.
# For multi-worker production, use Celery/Redis.
job_queue = deque()
job_results = {}
# A lock is crucial for safely modifying the queue and results from different threads.
queue_lock = threading.Lock()


# --- Flask App Setup ---
app = Flask(__name__)
app.config.from_object(Config)
CORS(app)  # Enable Cross-Origin Resource Sharing
# The default Flask logger is now intercepted by Loguru, so no app-specific setup is needed.


# --- Helper Functions ---
def is_allowed_file(filename):
    """Checks if the uploaded file has an allowed extension."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS
    )


def parse_request_args(form_data, image_file):
    """
    Parses and validates all incoming request parameters.
    Returns a dictionary of validated arguments or raises ValueError.
    """
    if not image_file or not is_allowed_file(image_file.filename):
        raise ValueError(
            "Invalid or no image file provided. Allowed types: "
            + ", ".join(Config.ALLOWED_EXTENSIONS)
        )

    try:
        input_image = Image.open(image_file.stream).convert("RGB")
    except UnidentifiedImageError:
        raise ValueError("The uploaded file is not a valid image.")

    try:
        args = {
            "image": input_image,
            "prompt": form_data.get("prompt", ""),
            "width": int(form_data.get("width", Config.DEFAULT_WIDTH)),
            "height": int(form_data.get("height", Config.DEFAULT_HEIGHT)),
            "num_inference_steps": int(
                form_data.get("num_inference_steps", Config.DEFAULT_STEPS)
            ),
            "guidance_scale": float(
                form_data.get("guidance_scale", Config.DEFAULT_GUIDANCE_SCALE)
            ),
            "true_cfg_scale": float(
                form_data.get("true_cfg_scale", Config.DEFAULT_TRUE_CFG_SCALE)
            ),
            "max_sequence_length": 512,  # Fixed advanced parameter
            "num_images_per_prompt": 1,
        }
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid parameter type provided. Please ensure all numerical fields are numbers. Details: {e}"
        )

    # Handle optional text prompts
    for key in ["prompt_2", "negative_prompt", "negative_prompt_2"]:
        value = form_data.get(key)
        if value:
            args[key] = value

    # Handle the seed for reproducibility
    seed_str = form_data.get("seed")
    if seed_str and seed_str.isdigit():
        seed = int(seed_str)
    else:
        seed = torch.randint(0, 2**32 - 1, (1,)).item()
    args["generator"] = torch.Generator(device=DEVICE).manual_seed(seed)
    args["seed_value"] = seed  # Store the actual seed used for status reporting

    return args


# --- Background Workers ---
def job_cleanup_worker():
    """
    Periodically cleans up old, completed jobs to free up memory and disk space.
    """
    logger.info("Job cleanup worker started.")
    while True:
        time.sleep(Config.CLEANUP_INTERVAL)
        with queue_lock:
            expired_jobs = []
            current_time = time.time()
            # A copy is needed to modify the dict while iterating
            all_job_ids = list(job_results.keys())

            for job_id in all_job_ids:
                job = job_results.get(job_id)
                # Ensure job exists and has a completion time
                if not job or "completion_time" not in job:
                    continue

                if (current_time - job["completion_time"]) > Config.JOB_RESULT_TTL:
                    expired_jobs.append(job_id)

            if expired_jobs:
                logger.info(f"Cleaning up {len(expired_jobs)} expired jobs...")
                for job_id in expired_jobs:
                    job_to_delete = job_results[job_id]
                    if "result_path" in job_to_delete:
                        try:
                            os.remove(job_to_delete["result_path"])
                            logger.debug(
                                f"Deleted result file: {job_to_delete['result_path']}"
                            )
                        except OSError as e:
                            logger.warning(
                                f"Could not delete result file {job_to_delete['result_path']}. Error: {e}"
                            )
                    del job_results[job_id]
                logger.info("Cleanup complete.")


def image_generation_worker():
    """
    The worker function that runs in a background thread.
    It continuously pulls jobs from the queue and processes them.
    """
    logger.info("Image generation worker started.")
    while True:
        job_id = None
        with queue_lock:
            if job_queue:
                job_id = job_queue.popleft()
                # Set status to 'processing'
                job_results[job_id]["status"] = "processing"
                job_results[job_id]["start_time"] = time.time()

        if job_id:
            pipe_kwargs = job_results[job_id]["params"]
            log_params = {
                k: v for k, v in pipe_kwargs.items() if k not in ["image", "generator"]
            }
            log_params["seed"] = job_results[job_id]["params"]["seed_value"]
            logger.info(f"Processing job {job_id} with params: {log_params}")
            pipe_kwargs.pop("seed_value")

            try:
                # --- Run the Image Processing Pipeline ---
                processed_image = pipe(**pipe_kwargs).images[0]

                # Save the image to a file on disk
                result_filename = f"{job_id}.png"
                result_path = os.path.join(Config.RESULTS_FOLDER, result_filename)
                processed_image.save(result_path, "PNG")

                # Store result and update status
                with queue_lock:
                    job_results[job_id].update(
                        {
                            "status": "completed",
                            "result_path": result_path,  # Store path instead of buffer
                            "completion_time": time.time(),
                        }
                    )
                logger.info(f"Job {job_id} completed successfully.")

            except torch.cuda.OutOfMemoryError as e:
                error_message = "Processing failed due to insufficient GPU memory. Try a smaller image size or reduce batch size."
                logger.error(f"Job {job_id} failed: {error_message} - {e}")
                with queue_lock:
                    job_results[job_id].update(
                        {"status": "failed", "error": error_message}
                    )
            except RuntimeError as e:
                # Catch other generic PyTorch/CUDA runtime errors
                error_message = (
                    f"A runtime error occurred during processing. Details: {e}"
                )
                logger.error(f"Job {job_id} failed: {error_message}")
                with queue_lock:
                    job_results[job_id].update(
                        {
                            "status": "failed",
                            "error": "An unexpected error occurred. This may be a resource issue.",
                        }
                    )
            except Exception as e:
                # Catch any other unexpected errors
                logger.exception(
                    f"An unexpected error occurred in worker for job {job_id}: {e}"
                )
                with queue_lock:
                    job_results[job_id].update(
                        {
                            "status": "failed",
                            "error": "An unexpected server error occurred.",
                        }
                    )
            finally:
                # Clean up large objects from the results dict to free memory
                if "params" in job_results.get(job_id, {}):
                    del job_results[job_id]["params"]["image"]
                    del job_results[job_id]["params"]["generator"]

        # Sleep to prevent busy-waiting when the queue is empty
        time.sleep(0.1)


# --- API Endpoints ---
@app.route("/process-image", methods=["POST"])
def generate_image_endpoint():
    """
    Accepts image generation requests, adds them to a queue,
    and returns a job ID for status polling.
    """
    with queue_lock:
        if len(job_queue) >= app.config["MAX_QUEUE_SIZE"]:
            logger.warning("Job queue is full. Rejecting new request.")
            return jsonify(
                {"error": "Server is currently busy. Please try again in a moment."}
            ), 503  # Service Unavailable

    if "image" not in request.files:
        return jsonify({"error": "No 'image' file part in the request."}), 400

    try:
        # This will raise ValueError for any invalid inputs
        pipe_kwargs = parse_request_args(request.form, request.files["image"])
    except ValueError as e:
        logger.warning(f"Bad request from {request.remote_addr}: {e}")
        return jsonify({"error": str(e)}), 400

    job_id = str(uuid.uuid4())
    with queue_lock:
        # Store parameters and add job to queue
        job_results[job_id] = {
            "status": "queued",
            "params": pipe_kwargs,
            "submit_time": time.time(),
        }
        job_queue.append(job_id)

    logger.info(f"Job {job_id} accepted and queued. Queue size: {len(job_queue)}")
    return jsonify(
        {
            "message": "Request accepted and queued.",
            "job_id": job_id,
            "status_url": f"/status/{job_id}",
            "result_url": f"/result/{job_id}",
        }
    ), 202  # Accepted


@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    """Provides the status of a specific job."""
    with queue_lock:
        job = job_results.get(job_id)

    if not job:
        return jsonify({"error": "Job ID not found."}), 404

    # Calculate queue position
    queue_pos = -1
    if job["status"] == "queued":
        try:
            # This needs to be in a try-except in case the job is dequeued
            # between getting the job status and checking the queue.
            queue_pos = list(job_queue).index(job_id) + 1
        except ValueError:
            # The job is no longer in the queue, its status might be updating.
            # The client can just poll again.
            pass

    response = {"job_id": job_id, "status": job.get("status")}
    if queue_pos != -1:
        response["queue_position"] = queue_pos
    if "error" in job:
        response["error"] = job["error"]

    return jsonify(response)


@app.route("/result/<job_id>", methods=["GET"])
def get_result(job_id):
    """Serves the generated image if the job is complete."""
    with queue_lock:
        job = job_results.get(job_id)

    if not job:
        return jsonify({"error": "Job ID not found."}), 404

    if job.get("status") == "completed":
        result_path = job.get("result_path")
        if result_path and os.path.exists(result_path):
            return send_file(result_path, mimetype="image/png")
        else:
            # This case should not happen if status is 'completed'
            return jsonify({"error": "Result for this job is missing."}), 500
    elif job.get("status") == "failed":
        return jsonify({"error": job.get("error", "An unknown error occurred.")}), 500
    else:
        return jsonify(
            {"error": f"Job is not yet complete. Current status: {job.get('status')}"}
        ), 202  # Accepted


# --- Custom Error Handlers for API ---
@app.errorhandler(404)
def not_found_error(error):
    return jsonify(
        {
            "error": "Not Found",
            "message": "The requested URL was not found on the server.",
        }
    ), 404


@app.errorhandler(405)
def method_not_allowed_error(error):
    return jsonify(
        {
            "error": "Method Not Allowed",
            "message": "The method is not allowed for the requested URL.",
        }
    ), 405


@app.errorhandler(413)
def payload_too_large_error(error):
    return jsonify(
        {
            "error": "Payload Too Large",
            "message": f"File upload is too large. Maximum size is {Config.MAX_UPLOAD_MB}MB.",
        }
    ), 413


@app.errorhandler(500)
def internal_server_error(error):
    logger.exception(f"Internal Server Error: {error}")
    return jsonify(
        {
            "error": "Internal Server Error",
            "message": "An unexpected error occurred on the server.",
        }
    ), 500


# --- Main Execution ---
if __name__ == "__main__":
    # Start the background worker thread.
    # The `daemon=True` ensures the thread will exit when the main program exits.
    worker_thread = threading.Thread(target=image_generation_worker, daemon=True)
    worker_thread.start()

    cleanup_thread = threading.Thread(target=job_cleanup_worker, daemon=True)
    cleanup_thread.start()

    # Serve the static frontend
    @app.route("/")
    def index():
        return send_file("static/index.html")

    logger.info("Starting Flask development server.")
    logger.warning(
        "This is a development server. For production, use a WSGI server like Gunicorn."
    )
    app.run(
        host="0.0.0.0", port=5000, debug=False
    )  # Debug mode should be False for this setup
