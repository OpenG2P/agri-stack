# ruff: noqa: E402
import logging

from .config import Settings

# Register this service's Settings first, so the framework (and its crypto
# module) read AGRI_COMPOSITE_* rather than its own defaults.
_config = Settings.get_config()

from fastapi import FastAPI
from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers.use_case_controller import UseCaseController
from .services.composite_service import CompositeService

_logger = logging.getLogger(_config.logging_default_logger_name)


class Initializer(BaseInitializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        CompositeService()
        UseCaseController().post_init()

    def init_logger(self):
        app_logger = super().init_logger()
        # The engine (framework-free) logs under "agri_composite.*": give it the
        # service's level and handlers.
        lg = logging.getLogger("agri_composite")
        lg.setLevel(getattr(logging, _config.logging_level))
        for handler in app_logger.handlers:
            if handler not in lg.handlers:
                lg.addHandler(handler)
        lg.propagate = False
        return app_logger

    def init_db(self):
        # Stateless service: no database engine.
        return None

    def migrate_database(self, args):
        _logger.info("agri-composite has no database; nothing to migrate")

    async def fastapi_app_startup(self, app: FastAPI):
        await CompositeService.get_component().start()

    async def fastapi_app_shutdown(self, app: FastAPI):
        await CompositeService.get_component().stop()
