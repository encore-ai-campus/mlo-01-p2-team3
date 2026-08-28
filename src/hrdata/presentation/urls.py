"""hrdata 대시보드 URL."""

from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("areas/", views.area_list, name="area_list"),
    path("areas/export.csv", views.area_export, name="area_export"),
    path("areas/<str:area_id>/", views.area_detail, name="area_detail"),
    path("organization-tree/", views.organization_tree, name="organization_tree"),
    path("managers/", views.manager_list, name="manager_list"),
    path("managers/export.csv", views.manager_export, name="manager_export"),
    path("managers/<str:manager_id>/", views.manager_detail, name="manager_detail"),
]
