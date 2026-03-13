from django.contrib import admin

from .models import ValorantMap


@admin.register(ValorantMap)
class ValorantMapAdmin(admin.ModelAdmin):
    list_display = ("display_name", "asset_name", "is_rank_map_pool", "updated_at")
    list_filter = ("is_rank_map_pool",)
    search_fields = ("display_name", "asset_name", "asset_path")
