from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter

from .views import CertificateJobViewSet, CertificateViewSet, TemplatePreviewView

router = DefaultRouter()
router.register("jobs", CertificateJobViewSet, basename="job")
router.register("certificates", CertificateViewSet, basename="certificate")

urlpatterns = [
    # OpenAPI Schema and Interactive Documentation (Swagger UI & Redoc)
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui-alt"),
    path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Template preview endpoint (instant PDF rendering without job persistence)
    path("template/preview/", TemplatePreviewView.as_view(), name="template-preview"),
] + router.urls
