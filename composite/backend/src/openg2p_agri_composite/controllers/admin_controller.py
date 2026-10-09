import asyncio
import os
from typing import Any, Dict, List, Optional

from fastapi import Query
from fastapi.responses import JSONResponse
from iam_core.user_auth.decorators import require_permissions
from openg2p_fastapi_common.controller import BaseController

from ..config import Settings
from ..core.activity import OUTCOMES
from ..core.catalogue import catalogue_url
from ..services.composite_service import CompositeService

_config = Settings.get_config()

VIEW = "composite:view"


def _not_found(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": {"code": code, "message": message}})


def _pm_reference(partner_id: str) -> str:
    return f"PARTNER_{partner_id.replace('-', '_').upper()}"


class AdminController(BaseController):
    """The console's read-only API (staff, IAM session with ``composite:view``).

    Configuration as loaded (use cases, registries, settings), each registry's
    data scope catalogue, the partners named by use cases with their keys in
    Partner Management, and the call log.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.prefix += "/composite/v1/admin"
        self.router.tags += ["Console"]
        for path, handler in (
            ("/overview", self.overview),
            ("/use-cases", self.list_use_cases),
            ("/use-cases/{use_case}", self.get_use_case),
            ("/registries", self.list_registries),
            ("/registries/{registry_id}/data-scopes", self.get_data_scopes),
            ("/partners", self.list_partners),
            ("/activity", self.list_activity),
        ):
            self.router.add_api_route(path, handler, methods=["GET"])

    # ── helpers ──────────────────────────────────────────────────────────────

    def _links(self) -> Dict[str, Optional[str]]:
        return {"pm_portal": _config.console_pm_portal_url or None, "cm_portal": _config.console_cm_portal_url or None}

    def _use_case_entry(self, compiled) -> Dict[str, Any]:
        return {**compiled.describe(), "allowed_partners": list(compiled.spec.allowed_partners),
                "file": os.path.basename(compiled.path)}

    def _partners(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for compiled in self.service.registry.published():
            for partner in compiled.spec.allowed_partners:
                out.setdefault(partner, []).append(compiled.ref)
        return dict(sorted(out.items()))

    def _used_by(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for compiled in self.service.registry.published():
            for controller in sorted({s.controller for s in compiled.spec.sources}):
                out.setdefault(controller, []).append(compiled.ref)
        return out

    # ── endpoints ────────────────────────────────────────────────────────────

    @require_permissions(VIEW)
    async def overview(self):
        """Settings, counts and the last day's calls."""
        activity = self.service.activity
        engine = self.service.engine.settings
        return {
            "composite_partner_id": engine.composite_partner_id,
            "consent_mode": engine.consent_mode,
            "exchange_cm_url": engine.exchange_cm_url if engine.consent_mode == "exchange" else "",
            "partner_mgmt_api_url": _config.partner_mgmt_api_url,
            "audit_manager_enabled": self.service.audit.enabled,
            "activity_recording": activity.enabled,
            "signing_kid": await self.service.signing_kid(),
            "use_cases": {"published": len(self.service.registry.published()),
                          "errors": len(self.service.registry.errors())},
            "registries": len(engine.registries),
            "partners": len(self._partners()),
            "activity_24h": await activity.summary(24) if activity.enabled else None,
            "links": self._links(),
        }

    @require_permissions(VIEW)
    async def list_use_cases(self):
        """Published use cases, with their allowed partners, and the files that failed to load."""
        return {
            "use_cases": [self._use_case_entry(c) for c in self.service.registry.published()],
            "errors": [{"file": f, "error": e} for f, e in sorted(self.service.registry.errors().items())],
        }

    @require_permissions(VIEW)
    async def get_use_case(self, use_case: str):
        """One published use case (``name`` or ``name@major``), with its mapping and file text."""
        try:
            compiled = self.service.registry.get(use_case)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": {"code": "invalid_use_case", "message": str(e)}})
        if compiled is None:
            return _not_found("unknown_use_case", f"no published use case '{use_case}'")
        try:
            with open(compiled.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None
        return {**self._use_case_entry(compiled), "mapping": dict(compiled.spec.response.mapping),
                "derived": dict(compiled.spec.response.derived), "source_yaml": text}

    @require_permissions(VIEW)
    async def list_registries(self):
        """Configured registries; reachability and scope count from their (cached) catalogues."""
        registries = self.service.engine.settings.registries
        used_by = self._used_by()
        results = await asyncio.gather(*(self.service.catalogue.fetch(c) for c in registries))
        out = []
        for (controller, endpoint), result in zip(registries.items(), results):
            ok = result["error"] is None
            out.append({
                "id": controller, "url": endpoint.get("url"), "catalogue_url": catalogue_url(endpoint),
                "partner_id": endpoint.get("partner_id") or "", "receiver_id": endpoint.get("receiver_id") or "",
                "reachable": ok, "scope_count": len(result["data_scopes"]) if ok else None,
                "error": result["error"], "used_by": used_by.get(controller, []),
            })
        return {"registries": out}

    @require_permissions(VIEW)
    async def get_data_scopes(self, registry_id: str, refresh: bool = False):
        """The registry's data scopes (current version's fields), and which use case sources use each."""
        if registry_id not in self.service.engine.settings.registries:
            return _not_found("unknown_registry", f"no registry '{registry_id}' is configured")
        result = await self.service.catalogue.fetch(registry_id, refresh=refresh)
        used: Dict[str, List[Dict[str, Any]]] = {}
        for compiled in self.service.registry.published():
            for src in compiled.spec.sources:
                for scope in src.scopes:
                    used.setdefault(scope, []).append({"use_case": compiled.ref, "source": src.id, "required": True})
                for scope in src.optional_scopes:
                    used.setdefault(scope, []).append({"use_case": compiled.ref, "source": src.id, "required": False})
        scopes = []
        for s in result["data_scopes"]:
            versions = s.get("versions") or []
            current = next((v for v in versions if v.get("version") == s.get("current_version")), None) or {}
            scope_id = s.get("scope_id") or f"{registry_id}.{s.get('name')}"
            scopes.append({
                "scope_id": scope_id, "name": s.get("name"), "label": s.get("label"),
                "description": s.get("description"), "status": s.get("status"),
                "current_version": s.get("current_version"),
                "fields": current.get("resolved_fields") or current.get("fields") or [],
                "used_by": used.get(scope_id, []),
            })
        return {"registry": registry_id, "data_controller": result["data_controller"],
                "fetched_at": result["fetched_at"], "error": result["error"], "data_scopes": scopes}

    @require_permissions(VIEW)
    async def list_partners(self):
        """Partners named in published use cases, with the keys Partner Management serves for them."""
        partners = self._partners()
        keys = await asyncio.gather(*(self.service.partner_keys(_pm_reference(p)) for p in partners if p != "*"))
        key_by_partner = dict(zip([p for p in partners if p != "*"], keys))
        out = []
        for partner, use_cases in partners.items():
            if partner == "*":
                out.append({"partner_id": "*", "pm_reference": None, "use_cases": use_cases,
                            "pm_keys": None, "pm_error": "any partner onboarded in Partner Management"})
                continue
            served, error = key_by_partner[partner]
            out.append({"partner_id": partner, "pm_reference": _pm_reference(partner), "use_cases": use_cases,
                        "pm_keys": served, "pm_error": error})
        return {"partners": out, "links": self._links()}

    @require_permissions(VIEW)
    async def list_activity(
        self,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        partner: Optional[str] = None,
        use_case: Optional[str] = None,
        outcome: Optional[str] = None,
    ):
        """The call log, newest first."""
        activity = self.service.activity
        if not activity.enabled:
            return {"recording": False, "total": 0, "items": []}
        if outcome and outcome not in OUTCOMES:
            return JSONResponse(status_code=400, content={"error": {
                "code": "invalid_outcome", "message": f"outcome must be one of {list(OUTCOMES)}"}})
        total, items = await activity.query(limit=limit, offset=offset, partner=partner,
                                            use_case=use_case, outcome=outcome)
        return {"recording": True, "total": total, "items": items}
