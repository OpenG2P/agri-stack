from typing import Any, Optional

from fastapi import Header, Request
from fastapi.responses import ORJSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..services.composite_service import CompositeService


class UseCaseController(BaseController):
    """Partner API: describe published use cases and query one.

    Trust is the partner-signed envelope verified against Partner Management
    keys (no Keycloak). The describe endpoints return configuration only.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.prefix += "/composite/v1"
        self.router.tags += ["Use cases"]

        self.router.add_api_route("/use-cases", self.list_use_cases, methods=["GET"])
        self.router.add_api_route("/use-cases/{use_case}", self.describe_use_case, methods=["GET"])
        self.router.add_api_route(
            "/use-cases/{use_case}/query",
            self.query,
            methods=["POST"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"example": {
                        "signature": "<detached JWS over {header, message}>",
                        "header": {"version": "1.0.0", "message_id": "…", "message_ts": "2026-10-01T10:00:00Z",
                                   "action": "query", "sender_id": "bank-a", "receiver_id": "agri-composite"},
                        "message": {"subject": {"type": "FAYDA_FAN", "value": "123456789012"},
                                    "parameters": {"crop_year": 2019, "season": "SEASON_MEHER"},
                                    "consent_jws": "<partner-signed consent with grants>"},
                    }}},
                }
            },
        )

    async def list_use_cases(self):
        """Published use cases: input, parameters, sources and output fields."""
        return {"use_cases": [c.describe() for c in self.service.registry.published()]}

    async def describe_use_case(self, use_case: str, x_use_case_major: Optional[int] = Header(None)):
        """One published use case (``name`` or ``name@major``)."""
        try:
            compiled = self.service.registry.get(use_case, x_use_case_major)
        except ValueError as e:
            return ORJSONResponse(status_code=400, content={"error": {"code": "invalid_use_case", "message": str(e)}})
        if compiled is None:
            return ORJSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_use_case", "message": f"no published use case '{use_case}'"}},
            )
        return compiled.describe()

    async def query(self, use_case: str, request: Request, x_use_case_major: Optional[int] = Header(None)):
        """Serve one query. Body: the signed envelope; response: the signed result envelope."""
        try:
            body: Any = await request.json()
        except Exception:
            body = None
        status, envelope = await self.service.engine.handle_query(use_case, body, x_use_case_major)
        return ORJSONResponse(status_code=status, content=envelope)
