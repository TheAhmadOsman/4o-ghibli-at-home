import os
import sys
import argparse
import torch
import io
import logging
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from loguru import logger
from PIL import Image, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# Import the Celery app instance and the task from your tasks.py file
from tasks import celery_app, generate_image_task

# --- Dotenv Configuration ---
# Loads environment variables from a .env file
load_dotenv()


# --- Logging Configuration (Loguru) ---
# This setup replaces Flask's default logger with the more powerful Loguru.
# It's configured to be less noisy by filtering out the frequent status check logs.
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)


class InterceptHandler(logging.Handler):
    """
    Custom logging handler to intercept standard logging messages
    and redirect them to Loguru.
    """

    def emit(self, record):
        # This is a filter to prevent spamming the log with status checks.
        if "GET /status/" in record.getMessage() and record.levelno == logging.INFO:
            return
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the correct stack frame to log from
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# Apply the custom handler to the root logger
logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


# --- Application Configuration ---
class Config:
    """
    Holds configuration settings for the Flask app, loaded from environment variables
    with sensible defaults.
    """

    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", 10))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
    RESULTS_FOLDER = os.getenv("RESULTS_FOLDER", "generated_images")
    CELERY_LOG_FILE = os.getenv("CELERY_LOG_FILE", "/app/celery_worker.log")
    # Default generation parameters (used as fallbacks)
    DEFAULT_WIDTH = 1024
    DEFAULT_HEIGHT = 1024
    DEFAULT_STEPS = 28
    DEFAULT_GUIDANCE_SCALE = 2.5
    DEFAULT_TRUE_CFG_SCALE = 1.0


# --- Flask App Setup ---
app = Flask(__name__)
app.config.from_object(Config)
CORS(app)  # Enable Cross-Origin Resource Sharing for the frontend

# Configure ProxyFix to correctly handle headers from a reverse proxy (like Nginx)
# This is important for production deployments.
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
)

# Create the directory for generated images if it doesn't already exist.
try:
    os.makedirs(Config.RESULTS_FOLDER, exist_ok=True)
    logger.info(f"Results will be saved in '{Config.RESULTS_FOLDER}' directory.")
except OSError as e:
    logger.critical(f"Could not create results directory. Error: {e}")
    sys.exit(1)


# --- Helper Functions ---
def is_allowed_file(filename):
    """Checks if the uploaded file has an allowed extension."""
    return (
        "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS
    )


def parse_request_args(form_data, image_file):
    """
    Parses and validates the incoming request form data and image file.
    Raises ValueError for invalid input.
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

    # Convert the uploaded image to bytes to pass to the Celery worker.
    # The worker will reconstruct the PIL Image from these bytes.
    with io.BytesIO() as output:
        input_image.save(output, format="PNG")
        image_bytes = output.getvalue()

    try:
        # Build the dictionary of arguments for the image generation pipeline
        args = {
            "image_bytes": image_bytes,
            "image_info": {"size": input_image.size, "mode": input_image.mode},
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
            "max_sequence_length": 512,
            "num_images_per_prompt": 1,
        }
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid parameter type provided. Please ensure all numerical fields are numbers. Details: {e}"
        ) from e

    # Add optional prompts if they were provided in the form data
    for key in ["prompt_2", "negative_prompt", "negative_prompt_2"]:
        value = form_data.get(key)
        if value:
            args[key] = value

    # Handle the seed value
    seed_str = form_data.get("seed")
    if seed_str and seed_str.isdigit():
        seed = int(seed_str)
    else:
        # Generate a random seed if none is provided
        seed = torch.randint(0, 2**32 - 1, (1,)).item()
    args["seed_value"] = seed

    return args


# --- API Endpoints ---
@app.route("/config", methods=["GET"])
def get_config():
    """Endpoint for the frontend to fetch its initial configuration."""
    return jsonify({"apiBaseUrl": ""})


@app.route("/process-image", methods=["POST"])
def generate_image_endpoint():
    """
    Accepts an image and parameters, then creates a background job with Celery.
    """
    if "image" not in request.files:
        return jsonify({"error": "No 'image' file part in the request."}), 400

    try:
        # Parse all arguments from the incoming request
        pipe_kwargs = parse_request_args(request.form, request.files["image"])
    except ValueError as e:
        logger.warning(f"Bad request from {request.remote_addr}: {e}")
        return jsonify({"error": str(e)}), 400

    # This is the key step: send the job to the Celery worker queue.
    # .delay() is the shortcut for .apply_async().
    task = generate_image_task.delay(pipe_kwargs, Config.RESULTS_FOLDER)

    logger.info(f"Job {task.id} accepted and sent to Celery worker.")

    # Respond immediately to the client with the job ID.
    return (
        jsonify(
            {
                "message": "Request accepted and queued for processing.",
                "job_id": task.id,
                "status_url": f"/status/{task.id}",
                "result_url": f"/result/{task.id}",
            }
        ),
        202,
    )


@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    """Provides the status of a specific Celery task."""
    task_result = celery_app.AsyncResult(job_id)
    response = {
        "job_id": job_id,
        "status": task_result.state,
    }
    if task_result.state == "FAILURE":
        response["error"] = str(task_result.info)
    elif task_result.state == "SUCCESS":
        response["result"] = task_result.result
    return jsonify(response)


@app.route("/result/<job_id>", methods=["GET"])
def get_result(job_id):
    """
    If a job is complete, this endpoint serves the resulting image file.
    """
    task_result = celery_app.AsyncResult(job_id)
    if not task_result.ready():
        return jsonify({"error": f"Job not complete. Status: {task_result.state}"}), 202

    if task_result.successful():
        result_data = task_result.get()
        result_path = result_data.get("result_path")
        if result_path and os.path.exists(result_path):
            return send_file(result_path, mimetype="image/png")
        else:
            return jsonify({"error": "Result file is missing."}), 500
    else:
        # If the task failed, return the error information.
        return jsonify({"error": str(task_result.info)}), 500


@app.route("/celery-log", methods=["GET"])
def get_celery_log():
    """Reads the last N lines of the Celery worker log file for debugging."""
    log_file_path = app.config["CELERY_LOG_FILE"]
    try:
        if os.path.exists(log_file_path):
            with open(log_file_path, "r") as f:
                # Read all lines and return the last 50 for a brief overview
                lines = f.readlines()
                log_content = "".join(lines[-50:])
                return jsonify({"log": log_content})
        else:
            return jsonify({"log": "Log file not found."})
    except Exception as e:
        logger.error(f"Could not read log file: {e}")
        return jsonify({"error": "Could not read log file."}), 500


@app.route("/")
def index():
    """Serves the main frontend HTML file."""
    return send_file("static/index.html")


# --- Custom Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Not Found"}), 404


@app.errorhandler(405)
def method_not_allowed_error(error):
    return jsonify({"error": "Method Not Allowed"}), 405


@app.errorhandler(413)
def payload_too_large_error(error):
    return jsonify({"error": f"Payload Too Large. Max size is {Config.MAX_UPLOAD_MB}MB."}), 413


@app.errorhandler(500)
def internal_server_error(error):
    logger.exception(f"Internal Server Error: {error}")
    return jsonify({"error": "An unexpected internal server error occurred."}), 500


# --- Main Execution Block ---
if __name__ == "__main__":
    # Set up an argument parser to allow specifying the port from the command line.
    parser = argparse.ArgumentParser(
        description="Run the Flask image generation server."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="The port to run the Flask application on (default: 5000)",
    )
    args = parser.parse_args()

    logger.info(f"Starting Flask development server on http://0.0.0.0:{args.port}.")
    logger.warning(
        "This is a development server. For production, use a WSGI server like Gunicorn."
    )

    # Run the Flask app. debug=False is important for a clean setup.
    app.run(host="0.0.0.0", port=args.port, debug=False)
