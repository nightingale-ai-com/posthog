from django.apps import AppConfig


class DiscordAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "products.discord_app.backend"
    label = "discord_app"

    def ready(self) -> None:
        # Import to register Django signal receivers (e.g. cache invalidation on Integration changes)
        import products.discord_app.backend.signals  # noqa: F401
