from fastapi import APIRouter
from app.api.v1.endpoints import auth, patients, cases, worklist, dashboard, referrals, patient_links, tasks, ai, messages, urgent

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(patients.router, prefix="/patients", tags=["Patients"])
api_router.include_router(cases.router, prefix="/cases", tags=["Cases"])
api_router.include_router(worklist.router, prefix="/worklist", tags=["Worklist"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
api_router.include_router(referrals.router, prefix="/referrals", tags=["Referrals"])
api_router.include_router(patient_links.router, prefix="/cases", tags=["Patient Links"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["Tasks"])
api_router.include_router(ai.router, prefix="/ai/suggestions", tags=["AI Suggestions"])
api_router.include_router(messages.router, prefix="/messages", tags=["Messages"])
api_router.include_router(urgent.case_router, prefix="/cases", tags=["Urgent Routing"])
api_router.include_router(urgent.router, prefix="/urgent-cases", tags=["Urgent Routing"])
