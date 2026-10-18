from typing import Optional
from pydantic import BaseModel


class DLQEntry(BaseModel):
    task_id: str
    user_id: int
    request_text: str
    error: str
    created_at: str


class RequeueResponse(BaseModel):
    original_task_id: str
    new_task_id: Optional[str]
    status: str
