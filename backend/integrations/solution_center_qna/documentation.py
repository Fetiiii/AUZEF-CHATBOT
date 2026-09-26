"""Public, integration-only OpenAPI and Swagger UI routes."""

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

from .router import router as integration_router


documentation_router = APIRouter(
    prefix=integration_router.prefix,
    include_in_schema=False,
)


@documentation_router.get("/openapi.json")
def integration_openapi():
    """Build a schema from only the three protected integration routes."""
    return get_openapi(
        title="AUZEF Çözüm Merkezi QnA Integration API",
        version="1.0.0",
        routes=integration_router.routes,
    )


@documentation_router.get("/docs")
def integration_docs():
    """Serve a key-free Swagger UI whose requests use the real API paths."""
    return get_swagger_ui_html(
        openapi_url=f"{integration_router.prefix}/openapi.json",
        title="AUZEF Çözüm Merkezi QnA Integration API - Swagger UI",
    )
