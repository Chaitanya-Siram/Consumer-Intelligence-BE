from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from exception_handlers import validation_exception_handler
from db_helpers.database import init_db
from routers import (
    upload_router, tagging_router, charts_router, agent_router,
    project_router, session_router,
    auth_router, user_router, organization_router, data_provider_keys_router
)

init_db()

app = FastAPI(title="PR Solutions", version="1.0.0")
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# CORS — allow the Vite dev server (and any origin) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# app.include_router(health_router)
app.include_router(auth_router)
app.include_router(organization_router)
app.include_router(user_router)
app.include_router(upload_router)
app.include_router(tagging_router)
app.include_router(charts_router)
app.include_router(agent_router)
app.include_router(project_router)
app.include_router(session_router)
app.include_router(data_provider_keys_router)
