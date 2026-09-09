from typing import Optional
from fastapi import Header, HTTPException, Query

class Page:
    def __init__(self, page: int = 1, size: int = 10):
        self.page = page
        self.size = size

def err(status_code: int, message: str):
    raise HTTPException(status_code=status_code, detail=message)

def get_current_user(authorization: Optional[str] = Header(None)):
    return {"user_id": 1, "role": "admin"}

def require_admin(current_user: dict = Header(None)):
    return True

def paginacion(page: int = Query(1, ge=1), size: int = Query(10, ge=1)):
    return Page(page=page, size=size)

def sobre(*args, **kwargs):
    # Función auxiliar para satisfacer la importación de admin_ops
    return True
