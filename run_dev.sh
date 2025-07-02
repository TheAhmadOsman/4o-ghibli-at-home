#!/bin/bash

# This script starts the development server and the ARQ worker.

# Exit immediately if a command exits with a non-zero status.
set -e

# Start the FastAPI server with Uvicorn
echo "Starting FastAPI server..."
uvicorn app.main:app --host 0.0.0.0 --port 5000 --reload &

# Start the ARQ worker
echo "Starting ARQ worker..."
arq app.services.task_queue.WorkerSettings