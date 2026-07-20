import inspect
import json
from pathlib import Path

import pytest
from selenium.webdriver.common.by import By

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


class _FakeElement:
    def __init__(self, *, tag_name="input", text="", attrs=None, displayed=True, children=None):
        self.tag_name = tag_name
        self.text = text
        self.attrs = attrs or {}
        self.displayed = displayed
        self.children = children or {}
        self.cleared = False
        self.sent_values = []

    def is_displayed(self):
        return self.displayed

    def get_attribute(self, name):
        return self.attrs.get(name)

    def clear(self):
        self.cleared = True

    def send_keys(self, value):
        self.sent_values.append(value)

    def find_element(self, by, selector):
        values = self.find_elements(by, selector)
        if not values:
            raise LookupError(selector)
        return values[0]

    def find_elements(self, by, selector):
        return self.children.get((by, selector), [])


class _FakeDriver:
    def __init__(self, control, label):
        self.control = control
        self.label = label

    def find_elements(self, by, selector):
        if by == By.XPATH and selector == "//input|//select|//textarea":
            return [self.control]
        if by == By.XPATH and selector.startswith("//label[@for="):
            return [self.label]
        if by == By.XPATH and selector == "//dt[normalize-space(.)!='' and following-sibling::dd[1]]":
            return []
        return []


def test_extract_label_value_fields_preserves_unknown_labeled_empty_control():
    control = _FakeElement(attrs={"id": "mystery_field", "value": ""})
    label = _FakeElement(tag_name="label", text="Campo Desconocido")
    driver = _FakeDriver(control, label)

    fields = client.extract_label_value_fields(driver)

    assert fields["Campo Desconocido"] == ""


def test_extract_label_value_fields_does_not_mutate_or_click_controls():
    source = inspect.getsource(client.extract_label_value_fields)

    assert ".click(" not in source
    assert ".clear(" not in source
    assert ".send_keys(" not in source


def test_grid_search_button_prefers_exact_read_only_id():
    class Access:
        def __init__(self):
            self.calls = []
            self.button = object()

        def find_element(self, by, selector):
            self.calls.append((by, selector))
            return self.button

    access = Access()

    assert client.find_grid_search_button(access) is access.button
    assert access.calls == [(By.ID, "ctl00_PageContent_Dms_Venta_VehiculoFilterButton__Button")]


def test_apply_grid_filters_sets_broad_date_window_and_folio(monkeypatch):
    from datetime import date

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 20)

    class Access:
        def __init__(self):
            self.from_date = _FakeElement()
            self.to_date = _FakeElement()
            self.folio_from = _FakeElement(attrs={"id": "ctl00_PageContent_FolioFromFilter", "value": ""})
            self.folio_to = _FakeElement(attrs={"id": "ctl00_PageContent_FolioToFilter", "value": ""})

        def find_elements(self, by, selector):
            if (by, selector) == (By.ID, "ctl00_PageContent_FechaFromFilter"):
                return [self.from_date]
            if (by, selector) == (By.ID, "ctl00_PageContent_FechaToFilter"):
                return [self.to_date]
            if (by, selector) == (By.ID, "ctl00_PageContent_FolioFromFilter"):
                return [self.folio_from]
            if (by, selector) == (By.ID, "ctl00_PageContent_FolioToFilter"):
                return [self.folio_to]
            return []

    access = Access()
    monkeypatch.setattr(client, "date", FixedDate)

    client.apply_grid_filters_for_folio(access, "7954")

    assert access.from_date.cleared is True
    assert access.from_date.sent_values == ["01-01-2010"]
    assert access.to_date.sent_values == ["20-07-2026"]
    assert access.folio_from.cleared is True
    assert access.folio_from.sent_values == ["7954"]
    assert access.folio_to.cleared is True
    assert access.folio_to.sent_values == ["7954"]


def test_element_metadata_excludes_password_and_redacts_client_filter_value():
    password = _FakeElement(attrs={"id": "PasswordFilter", "type": "password", "value": "secret"})
    client_filter = _FakeElement(
        attrs={"id": "ctl00_PageContent_ClienteFilter", "type": "text", "value": "CLIENTE PRIVADO"}
    )
    folio_filter = _FakeElement(attrs={"id": "ctl00_PageContent_FolioFilter", "type": "text", "value": "7954"})

    assert client.element_metadata(password, include_value=True) == {}
    assert client.element_metadata(client_filter, include_value=True)["value"] == "[redacted-if-present]"
    assert client.element_metadata(folio_filter, include_value=True)["value"] == "7954"


def test_safe_row_identifiers_keep_only_folio_and_actions():
    cells = ["7954", "CLIENTE PRIVADO", "Editar", "12.345.678-9", "Otro dato"]

    assert client.safe_row_identifiers(cells, "7954") == ["7954", "Editar"]


def test_extract_current_tab_bulk_snapshot_preserves_large_tables():
    rows = [["Header", "Value"]] + [[f"row-{index}", str(index)] for index in range(1, 121)]

    class Driver:
        def execute_script(self, script):
            assert "click" not in script
            assert "Guardar" not in script
            assert "Finalizar" not in script
            assert "querySelectorAll('select, textarea, input:not([type=button]):not([type=submit]):not([type=reset]):not([type=image])')" in script
            assert "querySelectorAll('button" not in script
            assert "querySelectorAll('input, select, textarea')" not in script
            assert "document.body ? document.body.innerText" not in script
            return {
                "fields": [{"label": "Comentario", "value": "texto"}, {"label": "Campo Vacio", "value": ""}],
                "tables": [{"index": 1, "rows": rows}],
                "text": "",
            }

    tab = client.extract_current_tab(Driver())

    assert tab["fields"]["Comentario"] == "texto"
    assert tab["fields"]["Campo Vacio"] == ""
    assert tab["text"] == ""
    assert len(tab["tables"][0]["rows"]) == 121
    assert tab["tables"][0]["rows"][-1] == ["row-120", "120"]
    assert all("truncated" not in " ".join(row).lower() for row in tab["tables"][0]["rows"])
