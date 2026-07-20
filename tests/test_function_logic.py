import json
from types import SimpleNamespace

from backend import function_logic
from backend.autopro_detail_client import AutoProSaleDetail


def make_event(args, widget_values=None):
    return SimpleNamespace(
        extra_params={
            "tool_calls": [{"args": args}],
            "widget_data": {"resolved_values": widget_values or ["user", "pass"]},
        },
        organization=SimpleNamespace(organization_id="org"),
        access_token="access",
    )


def payload_from_result(text):
    return json.loads(text.split("RESULT_JSON: ", 1)[1])


def test_process_request_blocks_non_milestone_folio(monkeypatch):
    client_created = False

    class UnexpectedClient:
        def __init__(self, **kwargs):
            nonlocal client_created
            client_created = True

    monkeypatch.setattr(function_logic, "AutoProSaleDetailClient", UnexpectedClient)

    result = function_logic.FunctionBackend(make_event({"folio": "7955"})).process_request()
    payload = payload_from_result(result)

    assert payload["status"] == "unavailable"
    assert payload["folio"] == "7955"
    assert "solo folio 7954" in payload["mensaje_tecnico"]
    assert client_created is False


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
                detalle_raw={"tabs": {"Resumen Venta": {"fields": {"Comentario": "texto"}, "tables": [], "text": "texto"}}},
                promoted={
                    "comentario": "texto",
                    "numero_chasis": "CH123",
                    "vin_or_unidad_id": "VIN987",
                    "uso_vehiculo": "Particular",
                    "tipo_venta_detalle": "Retail",
                    "forma_pago": {"fields": {"Forma Pago": "Contado"}, "tables": [], "text": "Contado"},
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
    assert payload["detalle_raw"]["tabs"]["Resumen Venta"]["fields"]["Comentario"] == "texto"


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
