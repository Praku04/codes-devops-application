from fastapi import APIRouter

from app.api.v1 import admin, auth, projects, settings

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(admin.router, tags=["admin"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(settings.router, tags=["settings"])
