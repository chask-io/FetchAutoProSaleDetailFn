import json
from types import SimpleNamespace

from backend import function_logic
from backend.autopro_detail_client import AutoProDetailUnavailableError, AutoProSaleDetail


def make_event(args, widget_values=None):
    return SimpleNamespace(
        extra_params={
            "tool_calls": [{"args": args}],
            "widget_data": {"resolved_values": widget_values or ["user", "pass"]},
        },
        organization=SimpleNamespace(organization_id="org"),
        access_token="access",
        orchestration_session_uuid="session-uuid",
        internal_orchestration_session_uuid="internal-session-uuid",
    )


def payload_from_result(text):
    return json.loads(text.split("RESULT_JSON: ", 1)[1])


def metadata_from_result(text):
    return json.loads(text.split("METADATA_JSON: ", 1)[1])


def fake_detail(folio):
    return AutoProSaleDetail(
        folio=str(folio),
        branch="698",
        browserbase_session_id=f"bb-{folio}",
        detalle_raw={
            "folio": str(folio),
            "tabs": {
                "Identificación Cliente": {"fields": {"Campo Desconocido": ""}, "tables": [], "text": ""},
                "Datos Vehículo": {"fields": {"Número Chasis": f"CH-{folio}"}, "tables": [], "text": ""},
                "Retoma": {"fields": {}, "tables": [], "text": ""},
                "Trámites": {"fields": {}, "tables": [], "text": ""},
                "Accesorios": {"fields": {}, "tables": [], "text": ""},
                "Datos Compra": {"fields": {}, "tables": [], "text": ""},
                "Forma de Pago": {"fields": {"Forma Pago": "Contado"}, "tables": [], "text": ""},
                "Resumen Venta": {
                    "fields": {"Comentario": f"comentario {folio}", "Bono Descuento": "$ 0"},
                    "tables": [{"index": 1, "rows": [["Concepto", "Monto"], ["Total", "$1"]]}],
                    "text": "",
                },
            },
            "tab_order": [
                "Identificación Cliente",
                "Datos Vehículo",
                "Retoma",
                "Trámites",
                "Accesorios",
                "Datos Compra",
                "Forma de Pago",
                "Resumen Venta",
            ],
        },
        promoted={
            "comentario": f"comentario {folio}",
            "numero_chasis": f"CH-{folio}",
            "vin_or_unidad_id": f"CH-{folio}",
            "uso_vehiculo": "Particular",
            "tipo_venta_detalle": "Retail",
            "forma_pago": {"fields": {"Forma Pago": "Contado"}, "tables": [], "text": ""},
            "bono_descuento": "$ 0",
            "dcto_recargo_pct": "0",
            "dcto_recargo_amount": "$0",
            "total_vehiculo_cliente": "$1",
            "fecha_entrega": "20-07-2026",
        },
    )


def test_process_request_legacy_single_folio_accepts_any_numeric_folio(monkeypatch):
    captured_kwargs = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def fetch_detail(self):
            return fake_detail(captured_kwargs["folio"])

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7955"})).process_request()
    payload = payload_from_result(result)

    assert payload["status"] == "success"
    assert payload["folio"] == "7955"
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["tables"][0]["rows"][1] == ["Total", "$1"]
    assert "file_uuid" not in payload


def test_process_request_returns_promoted_detail(monkeypatch):
    captured_kwargs = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def fetch_detail(self):
            return AutoProSaleDetail(
                folio="7954",
                branch="698",
                browserbase_session_id="bb-session",
                detalle_raw={
                    "tabs": {
                        "Forma de Pago": {
                            "fields": {"Forma Pago": "Contado"},
                            "tables": [{"index": 1, "rows": [["Medio", "Monto"], ["Contado", "$10"]]}],
                            "text": "",
                        },
                        "Resumen Venta": {
                            "fields": {
                                "Comentario": "texto",
                                "Bono Descuento": "",
                                "% Dcto Recargo": "0",
                                "Dcto Recargo": "$0",
                            },
                            "tables": [{"index": 2, "rows": [["Total", "$10"]]}],
                            "text": "",
                        },
                    },
                    "tab_order": ["Forma de Pago", "Resumen Venta"],
                },
                promoted={
                    "comentario": "texto",
                    "numero_chasis": "CH123",
                    "vin_or_unidad_id": "VIN987",
                    "uso_vehiculo": "Particular",
                    "tipo_venta_detalle": "Retail",
                    "forma_pago": {
                        "fields": {"Forma Pago": "Contado"},
                        "tables": [{"index": 1, "rows": [["Medio", "Monto"], ["Contado", "$10"]]}],
                        "text": "",
                    },
                    "bono_descuento": "$1",
                    "dcto_recargo_pct": "0",
                    "dcto_recargo_amount": "$0",
                    "total_vehiculo_cliente": "$10",
                    "fecha_entrega": "20-07-2026",
                },
            )

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7954", "node_id": "node"})).process_request()
    payload = payload_from_result(result)

    assert captured_kwargs["folio"] == "7954"
    assert captured_kwargs["branch"] == "698"
    assert captured_kwargs["browserbase_api_key"] == "bb-key"
    assert payload["status"] == "success"
    assert payload["folio"] == "7954"
    assert payload["folio_venta"] == "7954"
    assert payload["numero_chasis"] == "CH123"
    assert payload["vin_or_unidad_id"] == "VIN987"
    assert payload["comentario"] == "texto"
    assert payload["forma_pago"]["fields"]["Forma Pago"] == "Contado"
    assert payload["forma_pago"]["tables"][0]["rows"][1] == ["Contado", "$10"]
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["fields"]["Comentario"] == "texto"
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["fields"]["Bono Descuento"] == ""
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["tables"][0]["rows"][0] == ["Total", "$10"]
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["text"] == ""
    assert payload["diagnostico_respuesta"]["tab_body_text_serialized"] is False
    assert isinstance(payload["diagnostico_respuesta"]["serialized_bytes"], int)
    assert payload["diagnostico_respuesta"]["serialized_bytes"] > 0


def test_process_request_returns_structured_unavailable_on_live_error(monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            pass

        def fetch_detail(self):
            raise RuntimeError("portal timeout")

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FailingClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7954"})).process_request()
    payload = payload_from_result(result)

    assert payload["status"] == "unavailable"
    assert payload["folio"] == "7954"
    assert payload["folio_venta"] == "7954"
    assert payload["numero_chasis"] is None
    assert payload["vin_or_unidad_id"] is None
    assert payload["detalle_raw"] == {}
    assert payload["mensaje_tecnico"] == "portal timeout"


def test_process_request_includes_grid_diagnostics_on_unavailable(monkeypatch):
    class DiagnosticClient:
        def __init__(self, **kwargs):
            pass

        def fetch_detail(self):
            raise AutoProDetailUnavailableError(
                "grid miss",
                diagnostics={
                    "selected_context": {"branch": {"value": "698"}},
                    "filters_after": [{"id": "ctl00_PageContent_FolioFilter", "value": "7954"}],
                    "post_search": {"folio_visible": False, "empty_state": "no se encontraron"},
                },
            )

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", DiagnosticClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7954"})).process_request()
    payload = payload_from_result(result)

    assert payload["status"] == "unavailable"
    assert payload["diagnostico_grid"]["selected_context"]["branch"]["value"] == "698"
    assert payload["diagnostico_grid"]["filters_after"][0]["value"] == "7954"
    assert payload["diagnostico_grid"]["post_search"]["empty_state"] == "no se encontraron"


def test_empty_credentials_skip_browserbase(monkeypatch):
    client_created = False

    class UnexpectedClient:
        def __init__(self, **kwargs):
            nonlocal client_created
            client_created = True

    monkeypatch.setattr(function_logic, "resolve_autopro_credentials", lambda *a, **k: ("", ""))
    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", UnexpectedClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7954"})).process_request()
    payload = payload_from_result(result)

    assert payload["status"] == "unavailable"
    assert payload["mensaje_tecnico"] == "credenciales AutoPro no configuradas"
    assert client_created is False


def test_batch_processes_mixed_success_failure_uploads_collection_and_returns_small_contract(monkeypatch):
    calls = []
    sleeps = []
    uploaded = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.folio = kwargs["folio"]
            calls.append(kwargs)

        def fetch_detail(self):
            if self.folio == "9999":
                raise RuntimeError("folio no encontrado")
            return fake_detail(self.folio)

    class Files:
        def call(self, name, *args, **kwargs):
            assert name == "upload_file"
            file_obj = kwargs["file"]
            uploaded["name"] = file_obj.name
            uploaded["payload"] = json.loads(file_obj.getvalue().decode("utf-8"))
            uploaded["kwargs"] = kwargs
            return {"file_uuid": "batch-file-uuid", "status_code": 201}

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)
    monkeypatch.setattr(function_logic, "files_api_manager", Files())
    monkeypatch.setattr(function_logic.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = function_logic.FunctionBackend(
        make_event({"folios": ["7954", "9999"], "branch": "698", "delay_seconds": 0.25})
    ).process_request()
    contract = metadata_from_result(result)

    assert [call["folio"] for call in calls] == ["7954", "9999"]
    assert sleeps == [0.25]
    assert contract["status"] == "partial_success"
    assert contract["file_uuid"] == "batch-file-uuid"
    assert contract["counts"] == {"requested": 2, "success": 1, "failed": 1}
    assert contract["failures"] == [
        {"folio": "9999", "status": "unavailable", "mensaje_tecnico": "folio no encontrado"}
    ]
    assert "results" not in contract
    assert "detalle_raw" not in contract
    assert uploaded["name"] == "autopro_sale_detail_results.json"
    assert uploaded["kwargs"]["orchestration_session_uuids"] == ["session-uuid"]
    assert uploaded["payload"]["schema_version"] == "autopro_sale_detail_batch.v1"
    assert uploaded["payload"]["counts"] == contract["counts"]
    assert uploaded["payload"]["results"][0]["status"] == "success"
    assert uploaded["payload"]["results"][0]["detalle_raw"]["tabs"]["Resumen Venta"]["tables"][0]["rows"][1] == ["Total", "$1"]
    assert uploaded["payload"]["results"][0]["comentario"] == "comentario 7954"
    assert uploaded["payload"]["results"][0]["forma_pago"]["fields"]["Forma Pago"] == "Contado"
    assert uploaded["payload"]["results"][0]["bono_descuento"] == "$ 0"
    assert uploaded["payload"]["results"][1]["status"] == "unavailable"
    assert uploaded["payload"]["diagnostico_respuesta"]["serialized_bytes"] > contract["diagnostico_respuesta"]["serialized_bytes"]


def test_batch_loads_folios_from_generic_input_file_uuid(monkeypatch):
    requested_urls = []
    uploaded = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.folio = kwargs["folio"]

        def fetch_detail(self):
            return fake_detail(self.folio)

    class Files:
        def call(self, name, *args, **kwargs):
            if name == "get_all_files_for_session":
                return {"files": [{"file_uuid": "input-uuid", "file_url": "https://files/input.json"}]}
            if name == "upload_file":
                uploaded["payload"] = json.loads(kwargs["file"].getvalue().decode("utf-8"))
                return {"file_uuid": "output-uuid"}
            raise AssertionError(name)

    class Response:
        content = b'{"ventas": [{"folio": "7954"}, {"folio_venta": "7955"}, {"folio": "7954"}]}'

        def raise_for_status(self):
            return None

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)
    monkeypatch.setattr(function_logic, "files_api_manager", Files())
    monkeypatch.setattr(function_logic.requests, "get", lambda url, timeout: requested_urls.append((url, timeout)) or Response())
    monkeypatch.setattr(function_logic.time, "sleep", lambda seconds: None)

    result = function_logic.FunctionBackend(make_event({"file_uuid": "input-uuid", "delay_seconds": 0})).process_request()
    contract = metadata_from_result(result)

    assert requested_urls == [("https://files/input.json", 120)]
    assert contract["status"] == "success"
    assert contract["file_uuid"] == "output-uuid"
    assert contract["source_file_uuid"] == "input-uuid"
    assert contract["counts"] == {"requested": 3, "success": 3, "failed": 0}
    assert uploaded["payload"]["requested_folios"] == ["7954", "7955", "7954"]


def test_large_batch_payload_is_uploaded_not_relayed_inline(monkeypatch):
    large_rows = [["Concepto", "Monto"]] + [[f"item-{index}", "$1"] for index in range(200)]
    uploaded = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.folio = kwargs["folio"]

        def fetch_detail(self):
            detail = fake_detail(self.folio)
            detail.detalle_raw["tabs"]["Resumen Venta"]["tables"][0]["rows"] = large_rows
            return detail

    class Files:
        def call(self, name, *args, **kwargs):
            assert name == "upload_file"
            uploaded["bytes"] = len(kwargs["file"].getvalue())
            return {"file_uuid": "large-output-uuid"}

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)
    monkeypatch.setattr(function_logic, "files_api_manager", Files())
    monkeypatch.setattr(function_logic.time, "sleep", lambda seconds: None)

    result = function_logic.FunctionBackend(make_event({"folios": ["7954"], "delay_seconds": 0})).process_request()
    contract = metadata_from_result(result)

    assert contract["file_uuid"] == "large-output-uuid"
    assert contract["diagnostico_respuesta"]["result_collection_serialized_bytes"] == uploaded["bytes"]
    assert len(result.encode("utf-8")) < uploaded["bytes"] / 4
    assert "item-199" not in result


def test_folio_input_helpers_parse_lists_and_objects():
    assert function_logic.coerce_folio_values("7954, 7955\n7956") == ["7954", "7955", "7956"]
    assert function_logic.coerce_folio_values('["7954", "7955"]') == ["7954", "7955"]
    assert function_logic.extract_folios_from_payload({"items": [{"folio": 7954}, {"id": "7955"}]}) == [7954, "7955"]
    assert function_logic.dedupe_folios([" 7954 ", 7954, "7955"]) == ["7954", "7955"]


def test_batch_isolates_invalid_folio_values(monkeypatch):
    calls = []
    uploaded = {}

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs["folio"])

        def fetch_detail(self):
            return fake_detail(calls[-1])

    class Files:
        def call(self, name, *args, **kwargs):
            assert name == "upload_file"
            uploaded["payload"] = json.loads(kwargs["file"].getvalue().decode("utf-8"))
            return {"file_uuid": "invalid-folio-output"}

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", FakeClient)
    monkeypatch.setattr(function_logic, "files_api_manager", Files())
    monkeypatch.setattr(function_logic.time, "sleep", lambda seconds: None)

    result = function_logic.FunctionBackend(make_event({"folios": ["7954", "bad-folio"], "delay_seconds": 0})).process_request()
    contract = metadata_from_result(result)

    assert calls == ["7954"]
    assert contract["status"] == "partial_success"
    assert contract["counts"] == {"requested": 2, "success": 1, "failed": 1}
    assert contract["failures"][0]["folio"] == "bad-folio"
    assert "numeric" in contract["failures"][0]["mensaje_tecnico"]
    assert uploaded["payload"]["results"][1]["status"] == "unavailable"
