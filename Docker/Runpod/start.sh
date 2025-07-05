#!/bin/bash
# Exit immediately if a command exits with a non-zero status.
set -e

# --- SSH Setup (Mimicking your provided logic) ---
# This block sets up the SSH daemon for the root user using key-based auth.
echo "Setting up SSH daemon for key-based authentication..."

# Create the .ssh directory for the root user if it doesn't exist
mkdir -p /root/.ssh
# Set correct permissions for the .ssh directory (owner read/write/execute, others no access)
chmod 700 /root/.ssh

# RunPod injects the public key from your user settings into the $PUBLIC_KEY environment variable.
# Append this key to the authorized_keys file for the root user.
if [ -n "$PUBLIC_KEY" ]; then
    echo "$PUBLIC_KEY" >> /root/.ssh/authorized_keys
    # Set correct permissions for the authorized_keys file (owner read/write, others no access).
    chmod 600 /root/.ssh/authorized_keys
    echo "Public key added to authorized_keys."
else
    echo "WARNING: PUBLIC_KEY environment variable not set. Key-based SSH will not work."
fi

# Start the SSH service
service ssh start
echo "SSH daemon started."


# --- Dynamic .env File Creation ---
# This section creates the .env file from environment variables passed to the container.
echo "Generating .env file from environment variables..."
cat << EOF > .env
# This file is auto-generated at container startup by start.sh

# --- Application Configuration ---
MAX_UPLOAD_MB=${MAX_UPLOAD_MB:-10}
RESULTS_FOLDER=${RESULTS_FOLDER:-generated_images}

# --- Celery Configuration ---
# The URL for the Redis message broker.
REDIS_URL=${REDIS_URL:-redis://127.0.0.1:6379/0}

# --- Secrets ---
# IMPORTANT: Set this in your RunPod environment variables!
HUGGING_FACE_HUB_TOKEN=${HUGGING_FACE_HUB_TOKEN:-}
EOF
echo ".env file created successfully."


# --- Start Background Services ---
echo "Starting Redis server in the background..."
redis-server --daemonize yes
echo "Redis server started."

echo "Starting Celery worker in the background..."
# The '&' at the end runs this command as a background process.
# We direct output to a log file for easier debugging.
celery -A tasks.celery_app worker --loglevel=info --concurrency=1 > /app/celery_worker.log 2>&1 &
echo "Celery worker started. Logs are in /app/celery_worker.log"


# --- Start the Main Application with Gunicorn (Foreground Process) ---
# This is the final command and will run in the foreground.
# Gunicorn will now only handle fast API requests, so a long timeout isn't strictly necessary,
# but we keep it to prevent any issues with slow client connections.
echo "Starting Flask application with Gunicorn..."
exec gunicorn --workers 1 --threads 8 --timeout 600 -b 0.0.0.0:5000 app:app