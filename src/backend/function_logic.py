"""Business logic for FetchAutoProSaleDetailFn."""

import json
import logging
from typing import Any, Dict, Tuple

from chask_foundation.backend.models import OrchestrationEvent
from chask_foundation.configs.utils import get_secret

from .autopro_detail_client import (
    DEFAULT_BASE_URL,
    DEFAULT_BRANCH,
    AutoProSaleDetailClient,
    normalize_folio,
)
from .credentials import resolve_autopro_credentials

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_SLUG = "daniel-achondo"
AUTHORIZED_FIRST_MILESTONE_FOLIO = "7954"


class FunctionBackend:
    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        logger.info(
            "Initialized FetchAutoProSaleDetailFn for org: %s",
            orchestration_event.organization.organization_id,
        )

    def process_request(self) -> str:
        folio = ""
        branch = DEFAULT_BRANCH
        try:
            tool_args = self._extract_tool_args()
            folio = normalize_folio(tool_args.get("folio"))
            branch = str(tool_args.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
            verbose = bool(tool_args.get("verbose", False))

            if folio != AUTHORIZED_FIRST_MILESTONE_FOLIO:
                return format_result(
                    unavailable_result(
                        folio=folio,
                        branch=branch,
                        mensaje_tecnico=(
                            "batch/no-milestone gate activo: solo folio "
                            f"{AUTHORIZED_FIRST_MILESTONE_FOLIO} autorizado para este hito"
                        ),
                    )
                )

            username, password = resolve_autopro_credentials(self.orchestration_event)
            if not username or not password:
                return format_result(
                    unavailable_result(
                        folio=folio,
                        branch=branch,
                        mensaje_tecnico="credenciales AutoPro no configuradas",
                    )
                )

            browserbase_api_key, browserbase_project_id = get_browserbase_credentials()
            client = AutoProSaleDetailClient(
                username=username,
                password=password,
                base_url=DEFAULT_BASE_URL,
                browserbase_api_key=browserbase_api_key,
                browserbase_project_id=browserbase_project_id,
                folio=folio,
                branch=branch,
                verbose=verbose,
            )
            detail = client.fetch_detail()
            result = {
                "status": "success",
                "tenant_id": TENANT_SLUG,
                "folio": detail.folio,
                "folio_venta": detail.folio,
                "branch": detail.branch,
                "browserbase_session_id": detail.browserbase_session_id,
                "detalle_raw": detail.detalle_raw,
                **detail.promoted,
            }
            return format_result(result)
        except Exception as exc:
            logger.error("FetchAutoProSaleDetailFn unavailable: %s", exc, exc_info=True)
            return format_result(
                unavailable_result(
                    folio=folio,
                    branch=branch,
                    mensaje_tecnico=_safe_error_message(exc),
                    diagnostico_grid=getattr(exc, "diagnostics", None),
                )
            )

    def _extract_tool_args(self) -> Dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            return {}
        return tool_calls[0].get("args", {}) or {}


def get_browserbase_credentials() -> Tuple[str, str]:
    secret_value = get_secret("chask/browserbase", MODE="PRODUCTION")
    data = json.loads(secret_value)
    api_key = data.get("BROWSERBASE_API_KEY")
    project_id = data.get("BROWSERBASE_PROJECT_ID")
    if not api_key or not project_id:
        raise RuntimeError("Browserbase secret is missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID")
    return str(api_key), str(project_id)


def unavailable_result(
    *,
    folio: str,
    branch: str,
    mensaje_tecnico: str,
    diagnostico_grid: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    result = {
        "status": "unavailable",
        "tenant_id": TENANT_SLUG,
        "folio": folio or None,
        "folio_venta": folio or None,
        "branch": branch,
        "browserbase_session_id": None,
        "detalle_raw": {},
        "numero_chasis": None,
        "vin_or_unidad_id": None,
        "comentario": None,
        "uso_vehiculo": None,
        "tipo_venta_detalle": None,
        "forma_pago": None,
        "bono_descuento": None,
        "dcto_recargo_pct": None,
        "dcto_recargo_amount": None,
        "total_vehiculo_cliente": None,
        "fecha_entrega": None,
        "mensaje_tecnico": mensaje_tecnico,
    }
    if diagnostico_grid:
        result["diagnostico_grid"] = diagnostico_grid
    return result


def format_result(result: Dict[str, Any]) -> str:
    attach_response_diagnostics(result)
    if result.get("status") == "success":
        header = f"Detalle AutoPro extraido para folio {result.get('folio')}."
    else:
        header = f"Detalle AutoPro no disponible para folio {result.get('folio') or 'desconocido'}."
    return f"{header}\nRESULT_JSON: {json.dumps(result, ensure_ascii=False, sort_keys=True)}"


def attach_response_diagnostics(result: Dict[str, Any]) -> None:
    diagnostics = result.setdefault("diagnostico_respuesta", {})
    diagnostics["tab_body_text_serialized"] = False
    last_size = -1
    while True:
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        size = len(serialized)
        if size == last_size:
            diagnostics["serialized_bytes"] = size
            return
        diagnostics["serialized_bytes"] = size
        last_size = size


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    for sensitive in ("password", "contrasena", "contraseña", "secret", "token"):
        message = message.replace(sensitive, "[redacted]")
    return message[:500]
