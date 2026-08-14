"""Business logic for FetchAutoProSaleDetailFn."""

import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Tuple

import requests
from api.files_requests import files_api_manager
from chask_foundation.backend.models import OrchestrationEvent
from chask_foundation.configs.utils import get_secret

from .autopro_detail_client import (
    BROWSERBASE_MODE,
    DEFAULT_BASE_URL,
    DEFAULT_BRANCH,
    AutoProSaleDetailClient,
    normalize_folio,
    normalize_browser_mode,
)
from .credentials import resolve_autopro_credentials

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_SLUG = "daniel-achondo"
DEFAULT_BATCH_DELAY_SECONDS = 1.5
MAX_BATCH_DELAY_SECONDS = 30.0


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
            branch = str(tool_args.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
            verbose = bool(tool_args.get("verbose", False))
            folio_requests = self._resolve_requested_folios(tool_args)

            if not folio_requests:
                raise ValueError("Debe indicar folio, folios/folio_list o input_file_uuid/file_uuid.")

            username, password = resolve_autopro_credentials(self.orchestration_event)
            if not username or not password:
                folio = folio_requests[0]["folio"] if folio_requests else ""
                result = (
                    unavailable_result(
                        folio=folio,
                        branch=branch,
                        mensaje_tecnico="credenciales AutoPro no configuradas",
                    )
                )
                return format_result(result)

            browser_mode = normalize_browser_mode()
            browserbase_api_key = None
            browserbase_project_id = None
            if browser_mode == BROWSERBASE_MODE:
                browserbase_api_key, browserbase_project_id = get_browserbase_credentials()
            if is_batch_request(tool_args):
                return self._process_batch(
                    folio_requests=folio_requests,
                    branch=branch,
                    verbose=verbose,
                    username=username,
                    password=password,
                    browserbase_api_key=browserbase_api_key,
                    browserbase_project_id=browserbase_project_id,
                    browser_mode=browser_mode,
                    delay_seconds=parse_delay_seconds(tool_args.get("delay_seconds")),
                    source_file_uuid=tool_args.get("input_file_uuid") or tool_args.get("file_uuid") or None,
                    output_filename=tool_args.get("output_filename") or "autopro_sale_detail_results.json",
                )

            first_request = folio_requests[0]
            if first_request.get("error"):
                result = unavailable_result(
                    folio=first_request["folio"],
                    branch=branch,
                    mensaje_tecnico=first_request["error"],
                )
                return format_result(result)
            folio = first_request["folio"]
            result = self._fetch_one_result(
                folio=folio,
                branch=branch,
                verbose=verbose,
                username=username,
                password=password,
                browserbase_api_key=browserbase_api_key,
                browserbase_project_id=browserbase_project_id,
                browser_mode=browser_mode,
            )
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

    def _process_batch(
        self,
        *,
        folio_requests: List[Dict[str, str]],
        branch: str,
        verbose: bool,
        username: str,
        password: str,
        browserbase_api_key: str | None,
        browserbase_project_id: str | None,
        browser_mode: str,
        delay_seconds: float,
        source_file_uuid: Any,
        output_filename: str,
    ) -> str:
        started = time.time()
        results: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        client: AutoProSaleDetailClient | None = None

        try:
            for index, request in enumerate(folio_requests):
                if index:
                    time.sleep(delay_seconds)
                folio = request["folio"]
                logger.info(
                    "FetchAutoProSaleDetailFn batch folio start index=%s total=%s",
                    index + 1,
                    len(folio_requests),
                )
                try:
                    if request.get("error"):
                        result = unavailable_result(folio=folio, branch=branch, mensaje_tecnico=request["error"])
                    else:
                        if client is None:
                            client = self._create_client(
                                folio=folio,
                                branch=branch,
                                verbose=verbose,
                                username=username,
                                password=password,
                                browserbase_api_key=browserbase_api_key,
                                browserbase_project_id=browserbase_project_id,
                                browser_mode=browser_mode,
                            )
                            client.start_session_context()
                        result = self._fetch_one_result_with_client(
                            client=client,
                            folio=folio,
                            branch=branch,
                        )
                except Exception as exc:
                    logger.warning("AutoPro batch folio isolated exception folio=%s: %s", folio, exc, exc_info=True)
                    result = unavailable_result(
                        folio=folio,
                        branch=branch,
                        mensaje_tecnico=_safe_error_message(exc),
                        diagnostico_grid=getattr(exc, "diagnostics", None),
                    )
                results.append(result)
                if result.get("status") != "success":
                    if client is not None:
                        client.close()
                        client = None
                    failures.append(
                        {
                            "folio": result.get("folio"),
                            "status": result.get("status"),
                            "mensaje_tecnico": result.get("mensaje_tecnico"),
                        }
                    )
        finally:
            if client is not None:
                client.close()

        success_count = sum(1 for item in results if item.get("status") == "success")
        collection = {
            "schema_version": "autopro_sale_detail_batch.v1",
            "tenant_id": TENANT_SLUG,
            "branch": branch,
            "source_file_uuid": str(source_file_uuid) if source_file_uuid else None,
            "requested_folios": [request["folio"] for request in folio_requests],
            "counts": {
                "requested": len(folio_requests),
                "success": success_count,
                "failed": len(failures),
            },
            "failures": failures,
            "results": results,
        }
        attach_collection_diagnostics(collection)
        uploaded_bytes = json_payload_size(collection)
        file_uuid = self._upload_json(collection, output_filename)
        contract = {
            "status": "success" if not failures else "partial_success",
            "tenant_id": TENANT_SLUG,
            "branch": branch,
            "file_uuid": file_uuid,
            "result_file_uuid": file_uuid,
            "source_file_uuid": str(source_file_uuid) if source_file_uuid else None,
            "counts": collection["counts"],
            "failures": failures,
            "elapsed_ms": round((time.time() - started) * 1000),
            "diagnostico_respuesta": {
                "payload_uploaded": True,
                "result_collection_serialized_bytes": uploaded_bytes,
                "result_collection_compact_serialized_bytes": collection["diagnostico_respuesta"]["serialized_bytes"],
            },
        }
        logger.info(
            "FetchAutoProSaleDetailFn batch uploaded file_uuid=%s requested=%s success=%s failed=%s payload_bytes=%s",
            file_uuid,
            contract["counts"]["requested"],
            contract["counts"]["success"],
            contract["counts"]["failed"],
            contract["diagnostico_respuesta"]["result_collection_serialized_bytes"],
        )
        return format_batch_contract(contract)

    def _fetch_one_result(
        self,
        *,
        folio: str,
        branch: str,
        verbose: bool,
        username: str,
        password: str,
        browserbase_api_key: str | None,
        browserbase_project_id: str | None,
        browser_mode: str,
    ) -> Dict[str, Any]:
        try:
            client = self._create_client(
                folio=folio,
                branch=branch,
                verbose=verbose,
                username=username,
                password=password,
                browserbase_api_key=browserbase_api_key,
                browserbase_project_id=browserbase_project_id,
                browser_mode=browser_mode,
            )
            detail = client.fetch_detail()
            return detail_result_payload(detail)
        except Exception as exc:
            logger.warning("AutoPro detail unavailable for folio=%s: %s", folio, exc, exc_info=True)
            return unavailable_result(
                folio=folio,
                branch=branch,
                mensaje_tecnico=_safe_error_message(exc),
                diagnostico_grid=getattr(exc, "diagnostics", None),
            )

    @staticmethod
    def _create_client(
        *,
        folio: str,
        branch: str,
        verbose: bool,
        username: str,
        password: str,
        browserbase_api_key: str | None,
        browserbase_project_id: str | None,
        browser_mode: str,
    ) -> AutoProSaleDetailClient:
        return AutoProSaleDetailClient(
            username=username,
            password=password,
            base_url=DEFAULT_BASE_URL,
            browserbase_api_key=browserbase_api_key,
            browserbase_project_id=browserbase_project_id,
            browser_mode=browser_mode,
            folio=folio,
            branch=branch,
            verbose=verbose,
        )

    @staticmethod
    def _fetch_one_result_with_client(
        *,
        client: AutoProSaleDetailClient,
        folio: str,
        branch: str,
    ) -> Dict[str, Any]:
        detail = client.fetch_detail_for_folio(folio)
        payload = detail_result_payload(detail)
        payload["branch"] = payload.get("branch") or branch
        return payload

    def _resolve_requested_folios(self, tool_args: Dict[str, Any]) -> List[Dict[str, str]]:
        values: List[Any] = []
        for key in ("folios", "folio_list"):
            values.extend(coerce_folio_values(tool_args.get(key)))
        if not values and tool_args.get("folio") is not None:
            values.extend(coerce_folio_values(tool_args.get("folio")))

        input_file_uuid = tool_args.get("input_file_uuid") or tool_args.get("file_uuid")
        if input_file_uuid:
            payload = self._load_input_json(str(input_file_uuid))
            values.extend(extract_folios_from_payload(payload))
        return normalize_folio_requests(values, dedupe=False)

    def _load_input_json(self, requested_uuid: str) -> Any:
        info = self._find_session_file(requested_uuid)
        url = info.get("file_url")
        if not url:
            raise ValueError(f"El archivo {requested_uuid} no tiene URL de descarga.")
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        return json.loads(response.content.decode("utf-8"))

    def _find_session_file(self, requested_uuid: str) -> Dict[str, Any]:
        for info in self._list_session_files():
            if str(info.get("file_uuid") or info.get("uuid") or "") == str(requested_uuid):
                return info
        raise ValueError(f"No se encontró file_uuid={requested_uuid} en la sesión.")

    def _list_session_files(self) -> List[Dict[str, Any]]:
        response = files_api_manager.call(
            "get_all_files_for_session",
            orchestration_session_uuid=getattr(self.orchestration_event, "orchestration_session_uuid", None),
            internal_orchestration_session_uuid=getattr(
                self.orchestration_event,
                "internal_orchestration_session_uuid",
                None,
            ),
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        )
        if isinstance(response, dict):
            return response.get("files", []) or []
        return []

    def _upload_json(self, payload: Dict[str, Any], filename: str) -> str:
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        buffer = io.BytesIO(content)
        buffer.seek(0)
        buffer.name = filename

        result = files_api_manager.call(
            "upload_file",
            file=buffer,
            orchestration_session_uuids=[
                getattr(self.orchestration_event, "orchestration_session_uuid", None)
            ] if getattr(self.orchestration_event, "orchestration_session_uuid", None) else None,
            internal_orchestration_session_uuid=getattr(
                self.orchestration_event,
                "internal_orchestration_session_uuid",
                None,
            ),
            shared=False,
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        )
        file_data = result[0] if isinstance(result, list) and result else result
        if not isinstance(file_data, dict):
            raise RuntimeError(f"Respuesta de subida inesperada: {type(file_data)}")
        status = file_data.get("status_code")
        if status is not None and status not in (200, 201):
            raise RuntimeError(f"Falló la subida de {filename}: {file_data}")
        file_uuid = file_data.get("file_uuid") or file_data.get("uuid") or file_data.get("id")
        if not file_uuid:
            raise RuntimeError(f"La subida de {filename} no devolvió file_uuid: {file_data}")
        return str(file_uuid)

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


def coerce_folio_values(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return [item.strip() for item in stripped.replace("\n", ",").split(",") if item.strip()]
    return [value]


def extract_folios_from_payload(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        values: List[Any] = []
        for item in payload:
            if isinstance(item, dict):
                values.append(item.get("folio") or item.get("folio_venta") or item.get("id"))
            else:
                values.append(item)
        return values
    if not isinstance(payload, dict):
        raise ValueError("El archivo de entrada debe ser JSON lista u objeto.")
    for key in ("folios", "folio_list", "ventas", "sales", "items", "records"):
        if key in payload:
            return extract_folios_from_payload(payload[key])
    if payload.get("folio") or payload.get("folio_venta"):
        return [payload.get("folio") or payload.get("folio_venta")]
    raise ValueError("El archivo de entrada no contiene folios.")


def dedupe_folios(values: List[Any]) -> List[str]:
    return [request["folio"] for request in normalize_folio_requests(values, dedupe=True) if not request.get("error")]


def normalize_folio_requests(values: List[Any], *, dedupe: bool) -> List[Dict[str, str]]:
    requests_by_folio: List[Dict[str, str]] = []
    seen = set()
    for value in values:
        if value in (None, ""):
            continue
        raw = str(value).strip()
        try:
            folio = normalize_folio(value)
            error = ""
        except Exception as exc:
            folio = raw[:80] or "desconocido"
            error = _safe_error_message(exc)
        if dedupe and folio in seen:
            continue
        item = {"folio": folio}
        if error:
            item["error"] = error
        requests_by_folio.append(item)
        seen.add(folio)
    return requests_by_folio


def is_batch_request(tool_args: Dict[str, Any]) -> bool:
    if tool_args.get("input_file_uuid") or tool_args.get("file_uuid"):
        return True
    for key in ("folios", "folio_list"):
        values = coerce_folio_values(tool_args.get(key))
        if values:
            return True
    return bool(tool_args.get("batch"))


def parse_delay_seconds(value: Any) -> float:
    if value in (None, ""):
        value = os.environ.get("AUTOPRO_BATCH_DELAY_SECONDS", DEFAULT_BATCH_DELAY_SECONDS)
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = DEFAULT_BATCH_DELAY_SECONDS
    return max(0.0, min(delay, MAX_BATCH_DELAY_SECONDS))


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
        "bono_descuento_pct": None,
        "dcto_recargo_pct": None,
        "dcto_recargo_amount": None,
        "total_vehiculo_cliente": None,
        "fecha_entrega": None,
        "mensaje_tecnico": mensaje_tecnico,
    }
    if diagnostico_grid:
        result["diagnostico_grid"] = diagnostico_grid
    return result


def detail_result_payload(detail) -> Dict[str, Any]:
    return {
        "status": "success",
        "tenant_id": TENANT_SLUG,
        "folio": detail.folio,
        "folio_venta": detail.folio,
        "branch": detail.branch,
        "browserbase_session_id": detail.browserbase_session_id,
        "detalle_raw": detail.detalle_raw,
        **detail.promoted,
    }


def format_result(result: Dict[str, Any]) -> str:
    attach_response_diagnostics(result)
    logger.info(
        "FetchAutoProSaleDetailFn RESULT_JSON serialized_bytes=%s status=%s",
        result["diagnostico_respuesta"]["serialized_bytes"],
        result.get("status"),
    )
    if result.get("status") == "success":
        header = f"Detalle AutoPro extraido para folio {result.get('folio')}."
    else:
        header = f"Detalle AutoPro no disponible para folio {result.get('folio') or 'desconocido'}."
    return f"{header}\nRESULT_JSON: {json.dumps(result, ensure_ascii=False, sort_keys=True)}"


def format_batch_contract(contract: Dict[str, Any]) -> str:
    attach_response_diagnostics(contract)
    logger.info(
        "FetchAutoProSaleDetailFn BATCH_CONTRACT_JSON serialized_bytes=%s status=%s",
        contract["diagnostico_respuesta"]["serialized_bytes"],
        contract.get("status"),
    )
    counts = contract["counts"]
    header = (
        "Batch AutoPro completado."
        if contract.get("status") == "success"
        else "Batch AutoPro completado con fallas aisladas."
    )
    return (
        f"{header}\n"
        f"Folios: {counts['requested']} | exitosos: {counts['success']} | fallidos: {counts['failed']}.\n"
        f"file_uuid: {contract['file_uuid']}\n"
        f"METADATA_JSON: {json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
    )


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


def attach_collection_diagnostics(collection: Dict[str, Any]) -> None:
    diagnostics = collection.setdefault("diagnostico_respuesta", {})
    diagnostics["tab_body_text_serialized"] = False
    last_size = -1
    while True:
        serialized = json.dumps(collection, ensure_ascii=False, sort_keys=True).encode("utf-8")
        size = len(serialized)
        if size == last_size:
            diagnostics["serialized_bytes"] = size
            return
        diagnostics["serialized_bytes"] = size
        last_size = size


def json_payload_size(payload: Dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    for sensitive in ("password", "contrasena", "contraseña", "secret", "token"):
        message = message.replace(sensitive, "[redacted]")
    return message[:500]
