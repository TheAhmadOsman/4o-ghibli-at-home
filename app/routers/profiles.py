import json
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException
from app.models.profile import Profile

router = APIRouter()

PROFILES_FILE = Path("static/profiles.json")


@router.get("/profiles", response_model=List[Profile])
async def get_profiles():
    if not PROFILES_FILE.is_file():
        raise HTTPException(status_code=404, detail="Profiles file not found.")

    with open(PROFILES_FILE, "r") as f:
        profiles_data = json.load(f)

    return profiles_data
