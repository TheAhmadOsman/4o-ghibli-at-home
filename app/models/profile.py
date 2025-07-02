from pydantic import BaseModel, Field
from typing import List


class Profile(BaseModel):
    id: str
    name: str
    preview: str
    tags: List[str] = Field(default_factory=list)
    model_id: str
    lora: str
    seed: int
    prompt: str
    negative_prompt: str
