from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class JobSubmissionResponse(BaseModel):
    """Response model for a successfully submitted job."""

    message: str = "Request accepted and queued."
    job_id: str
    status_url: str
    result_url: str


class JobResult(BaseModel):
    """Detailed status and result of a job."""

    job_id: str
    status: str
    queue_position: Optional[int] = Field(
        None, description="The job's current position in the queue (if queued)."
    )
    result: Optional[Dict[str, Any]] = Field(
        None,
        description="The result of the job if completed, or error details if failed.",
    )
    enqueue_time: datetime
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None


class Job(BaseModel):
    """Represents a job in the system."""

    job_id: str
    status: str = "queued"
    params: Dict[str, Any]
    submit_time: float
    start_time: Optional[float] = None
    completion_time: Optional[float] = None
    result_path: Optional[str] = None
    error: Optional[str] = None
