from .admin import router as admin_router
from .assessment import router as assessment_router
from .general import router as general_router
from .trainer import router as trainer_router

__all__ = [
    "general_router",
    "trainer_router",
    "assessment_router",
    "admin_router",
]
