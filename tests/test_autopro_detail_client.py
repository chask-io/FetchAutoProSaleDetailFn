import json
from pathlib import Path

import pytest

from backend import autopro_detail_client as client


def test_read_only_guard_rejects_save_and_finalize_terms():
    for unsafe in ["Guardar", "//button[contains(., 'Finalizar')]", "finalizar venta"]:
        with pytest.raises(client.AutoProReadOnlyViolation):
            client.assert_read_only_selector(unsafe)


def test_detail_client_source_does_not_query_for_unsafe_controls():
    source = Path(client.__file__).read_text()
    forbidden_fragments = [
        "contains(., 'Guardar')",
        "contains(., 'Finalizar')",
        "@value='Guardar'",
        "@value='Finalizar'",
    ]

    assert all(fragment not in source for fragment in forbidden_fragments)


def test_promote_detail_preserves_forma_pago_rows_and_resumen():
    raw = {
        "tabs": {
            "Identificacion Cliente": {
                "fields": {"Uso Vehiculo": "Particular", "Tipo Venta": "Retail", "Numero Chasis": "CH123"},
                "tables": [],
                "text": "",
            },
            "Datos Vehiculo": {
                "fields": {"VIN": "VIN987", "Codigo Interno": "INT456"},
                "tables": [],
                "text": "",
            },
            "Forma de Pago": {
                "fields": {"Forma Pago": "Credito Convencional", "Cuotas": "36"},
                "tables": [{"index": 1, "rows": [["Concepto", "Monto"], ["Pie", "$1.000.000"]]}],
                "text": "Forma Pago Credito Convencional",
            },
            "Resumen Venta": {
                "fields": {
                    "Comentario": "M1, devolver bono flota",
                    "Bono Descuento": "$300.000",
                    "% Dcto Recargo": "0,00",
                    "Dcto Recargo": "$0",
                    "Total Vehiculo Cliente": "$12.345.678",
                    "Fecha Entrega": "20-07-2026",
                },
                "tables": [{"index": 2, "rows": [["Total", "$12.345.678"]]}],
                "text": "Comentario M1, devolver bono flota",
            },
        }
    }

    promoted = client.promote_detail(raw)

    assert promoted["comentario"] == "M1, devolver bono flota"
    assert promoted["numero_chasis"] == "CH123"
    assert promoted["vin_or_unidad_id"] == "VIN987"
    assert promoted["uso_vehiculo"] == "Particular"
    assert promoted["tipo_venta_detalle"] == "Retail"
    assert promoted["bono_descuento"] == "$300.000"
    assert promoted["dcto_recargo_pct"] == "0,00"
    assert promoted["dcto_recargo_amount"] == "$0"
    assert promoted["total_vehiculo_cliente"] == "$12.345.678"
    assert promoted["fecha_entrega"] == "20-07-2026"
    assert promoted["forma_pago"]["fields"]["Forma Pago"] == "Credito Convencional"
    assert promoted["forma_pago"]["tables"][0]["rows"][1] == ["Pie", "$1.000.000"]
    assert promoted["resumen_venta"]["tables"][0]["rows"][0] == ["Total", "$12.345.678"]


def test_normalize_folio_requires_numeric_value():
    assert client.normalize_folio(" 7954 ") == "7954"
    with pytest.raises(ValueError):
        client.normalize_folio("7954-A")


def test_result_payload_is_json_serializable():
    detail = client.AutoProSaleDetail(
        folio="7954",
        branch="698",
        browserbase_session_id="session",
        detalle_raw={"tabs": {"Resumen Venta": {"fields": {"Comentario": "ok"}, "tables": [], "text": ""}}},
        promoted={"comentario": "ok"},
    )

    json.dumps(detail.__dict__, ensure_ascii=False)


def test_promote_detail_falls_back_to_codigo_interno_for_identity_key():
    raw = {
        "tabs": {
            "Datos Vehiculo": {
                "fields": {"Codigo Interno": "UNI-7954"},
                "tables": [],
                "text": "",
            }
        }
    }

    promoted = client.promote_detail(raw)

    assert promoted["numero_chasis"] is None
    assert promoted["vin_or_unidad_id"] == "UNI-7954"
