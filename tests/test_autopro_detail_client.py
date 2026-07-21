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


def test_promote_detail_uses_autopro_precio_venta_descuento_aliases():
    raw = raw_with_production_resumen_bonus(
        pct="-12,09",
        amount="$ -2.962.148",
        internal_bonus="$ -1",
        resumen_bonus="$ 0",
        resumen_bonus_pct="0%",
    )

    promoted = client.promote_detail(raw)

    assert promoted["dcto_recargo_amount"] == "$ -2.962.148"
    assert promoted["dcto_recargo_pct"] == "-12,09"
    assert promoted["bono_descuento"] == "$ 0"
    assert promoted["bono_descuento_pct"] == "0%"


@pytest.mark.parametrize(
    ("folio", "pct", "amount", "internal_bonus", "resumen_bonus", "resumen_bonus_pct"),
    [
        ("7954", "-12,09", "$ -2.962.148", "$ 0", "$ 0", "0%"),
        ("7993", "-0,73", "$ -200.000", "$ -2.500.000", "$ 2.500.000", "-8,34%"),
        ("7997", "-3,54", "$ -883.100", "$ -4.760.000", "$ 4.760.000", "-16,01%"),
    ],
)
def test_promote_detail_prefers_production_shape_resumen_bono_over_internal_bono_field(
    folio,
    pct,
    amount,
    internal_bonus,
    resumen_bonus,
    resumen_bonus_pct,
):
    raw = raw_with_production_resumen_bonus(
        pct=pct,
        amount=amount,
        internal_bonus=internal_bonus,
        resumen_bonus=resumen_bonus,
        resumen_bonus_pct=resumen_bonus_pct,
        folio=folio,
    )

    promoted = client.promote_detail(raw)

    assert promoted["dcto_recargo_pct"] == pct
    assert promoted["dcto_recargo_amount"] == amount
    assert promoted["bono_descuento"] == resumen_bonus
    assert promoted["bono_descuento_pct"] == resumen_bonus_pct


def test_promote_detail_uses_raw_bono_fallback_only_when_no_exact_resumen_row():
    raw = raw_with_production_resumen_bonus(
        pct="-0,73",
        amount="$ -200.000",
        internal_bonus="$ -2.500.000",
        resumen_bonus="$ 2.500.000",
        resumen_bonus_pct="-8,34%",
    )
    raw["tabs"]["Resumen Venta"]["tables"] = [
        {
            "index": 1,
            "rows": [
                ["", "Bono Descuento Totalizado", "$ 2.500.000", "-8,34%"],
                ["", "Dscto. o Recargo", "$ -200.000", "-0,73%"],
            ],
        }
    ]

    promoted = client.promote_detail(raw)

    assert promoted["bono_descuento"] == "$ -2.500.000"
    assert promoted["bono_descuento_pct"] is None


def raw_with_production_resumen_bonus(
    *,
    pct,
    amount,
    internal_bonus,
    resumen_bonus,
    resumen_bonus_pct,
    folio="7954",
):
    return {
        "tabs": {
            "Datos Vehículo": {
                "fields": {
                    "ctl00$PageContent$WizardPanels$Precio_Venta_Descuento_Pje": pct,
                    "ctl00$PageContent$WizardPanels$Precio_Venta_Descuento": amount,
                    "ctl00$PageContent$WizardPanels$Bono_DR_LP": internal_bonus,
                },
                "tables": [],
                "text": "",
            },
            "Resumen Venta": {
                "fields": {},
                "tables": [
                    {
                        "index": 1,
                        "rows": [
                            [
                                f"EDITAR VENTA VEHICULO - FOLIO : {folio} Resumen Venta "
                                f"Vehiculo % Precio Lista Bono Descuento {resumen_bonus} {resumen_bonus_pct}"
                            ],
                            ["", "", "Vehiculo", "%", "Accesorios", "%", "Tramites", "%", "Contratos", "%", "Total", "%"],
                            ["", "Precio Lista", "$ 29.980.000", "", "", "", "", "", "", "", "$ 29.980.000", ""],
                            ["", "Bono Descuento", resumen_bonus, resumen_bonus_pct, "", "", "", "", "", "", resumen_bonus, resumen_bonus_pct],
                            ["", "Venta", "$ 27.480.000", "", "$ 0", "", "$ 0", "", "$ 0", "", "$ 27.480.000", ""],
                            ["", "Dscto. o Recargo", amount, f"{pct}%", "$ 0", "0%", "$ 0", "0%", "$ 0", "0%", amount, f"{pct}%"],
                        ],
                    },
                    {
                        "index": 2,
                        "rows": [
                            ["", "Concepto", "Vehiculo", "%"],
                            ["", "Bono Descuento", "$ 999.999.999", "-99,99%"],
                        ],
                    },
                ],
                "text": "",
            },
        }
    }


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


def test_extract_current_tab_bulk_snapshot_scopes_active_root_and_preserves_large_tables():
    active_rows = [["Header", "Value"]] + [[f"row-{index}", str(index)] for index in range(1, 121)]
    hidden_sibling_rows = [["Hidden", "Value"]] + [[f"hidden-{index}", str(index)] for index in range(1, 200)]

    class Driver:
        def __init__(self):
            self.target_fragment = None

        def execute_script(self, script, target_fragment="", include_tables=True):
            self.target_fragment = target_fragment
            self.include_tables = include_tables
            assert "click" not in script
            assert "Guardar" not in script
            assert "Finalizar" not in script
            assert "querySelectorAll('select, textarea, input:not([type=button]):not([type=submit]):not([type=reset]):not([type=image])')" in script
            assert "querySelectorAll('button" not in script
            assert "querySelectorAll('input, select, textarea')" not in script
            assert "root.querySelectorAll('table')" in script
            assert "document.querySelectorAll('table')" not in script
            assert "document.body ? document.body.innerText" not in script
            return {
                "fields": [{"label": "Comentario", "value": "texto"}, {"label": "Campo Vacio", "value": ""}],
                "tables": [{"index": 1, "rows": active_rows}],
                "text": "",
                "meta": {
                    "root_source": "direct-target",
                    "root_tag": "div",
                    "root_id": "panel-resumen",
                    "root_class": "tab-pane active",
                    "fields_count": 2,
                    "tables_count": 1,
                    "rows_count": len(active_rows),
                    "cells_count": sum(len(row) for row in active_rows),
                },
            }

    driver = Driver()
    tab = client.extract_current_tab(
        driver,
        active_tab={
            "tag": "a",
            "id": "tab-resumen-link",
            "class": "active",
            "aria_controls": "panel-resumen",
            "href_fragment": "",
            "source": "[aria-selected=\"true\"][aria-controls]",
        },
    )

    assert driver.target_fragment == "panel-resumen"
    assert driver.include_tables is True
    assert tab["fields"]["Comentario"] == "texto"
    assert tab["fields"]["Campo Vacio"] == ""
    assert tab["text"] == ""
    assert tab["_snapshot_meta"]["root_source"] == "direct-target"
    assert tab["_snapshot_meta"]["active_id"] == "tab-resumen-link"
    assert tab["_snapshot_meta"]["active_aria_controls"] == "panel-resumen"
    assert len(hidden_sibling_rows) == 200
    assert len(tab["tables"][0]["rows"]) == 121
    assert tab["tables"][0]["rows"][-1] == ["row-120", "120"]
    assert all("hidden-" not in " ".join(row) for row in tab["tables"][0]["rows"])
    assert all("truncated" not in " ".join(row).lower() for row in tab["tables"][0]["rows"])


def test_extract_current_tab_direct_target_prefers_aria_controls_over_href():
    class Driver:
        def __init__(self):
            self.target_fragment = None

        def execute_script(self, script, target_fragment="", include_tables=True):
            self.target_fragment = target_fragment
            self.include_tables = include_tables
            assert "const directPanel = panelFromId(targetFragment)" in script
            assert "root_source: rootChoice.source" in script
            return {
                "fields": [{"label": "Campo", "value": "valor"}],
                "tables": [],
                "text": "",
                "meta": {"root_source": "direct-target", "root_id": "panel-aria"},
            }

    driver = Driver()

    tab = client.extract_current_tab(
        driver,
        active_tab={
            "tag": "a",
            "id": "tab-active",
            "class": "active",
            "aria_controls": "panel-aria",
            "href_fragment": "panel-href",
            "source": "unit",
        },
    )

    assert driver.target_fragment == "panel-aria"
    assert driver.include_tables is True
    assert tab["fields"]["Campo"] == "valor"
    assert tab["_snapshot_meta"]["root_source"] == "direct-target"
    assert tab["_snapshot_meta"]["root_id"] == "panel-aria"
    assert tab["_snapshot_meta"]["active_id"] == "tab-active"
    assert tab["_snapshot_meta"]["active_source"] == "unit"


def test_extract_current_tab_logs_fallback_source_when_no_direct_target():
    class Driver:
        def __init__(self):
            self.target_fragment = None

        def execute_script(self, script, target_fragment="", include_tables=True):
            self.target_fragment = target_fragment
            self.include_tables = include_tables
            assert "fallback-active-target" in script
            return {
                "fields": [{"label": "Campo", "value": ""}],
                "tables": [],
                "text": "",
                "meta": {
                    "root_source": "fallback-active-target",
                    "active_id": "fallback-tab",
                    "active_href_fragment": "fallback-panel",
                },
            }

    driver = Driver()

    tab = client.extract_current_tab(driver, active_tab={})

    assert driver.target_fragment == ""
    assert driver.include_tables is True
    assert tab["fields"]["Campo"] == ""
    assert tab["_snapshot_meta"]["root_source"] == "fallback-active-target"
    assert tab["_snapshot_meta"]["active_id"] == "fallback-tab"
    assert tab["_snapshot_meta"]["active_href_fragment"] == "fallback-panel"


def test_extract_current_tab_can_skip_non_resumen_layout_tables_and_preserve_textarea_verbatim():
    class Driver:
        def __init__(self):
            self.include_tables = None

        def execute_script(self, script, target_fragment="", include_tables=True):
            self.include_tables = include_tables
            assert "const includeTables = Boolean(arguments[1]);" in script
            assert "tag === 'textarea'" in script
            huge_rows = [["Header", "Value"]] + [[f"layout-{index}", str(index)] for index in range(1, 200)]
            return {
                "fields": [{"label": "Comentario", "value": "linea 1\n  linea 2  "}],
                "tables": [{"index": 1, "rows": huge_rows}] if include_tables else [],
                "text": "",
                "meta": {"root_source": "form"},
            }

    driver = Driver()

    tab = client.extract_current_tab(driver, include_tables=False)

    assert driver.include_tables is False
    assert tab["fields"]["Comentario"] == "linea 1\n  linea 2  "
    assert tab["tables"] == []


def test_extract_wizard_uses_fixed_step_names_and_only_keeps_resumen_tables(monkeypatch):
    huge_resumen_rows = [["Concepto", "Monto"]] + [[f"item-{index}", str(index)] for index in range(1, 150)]
    extract_calls = []
    clicks = []

    class Deadline:
        def raise_if_expired(self):
            return None

        def remaining_seconds(self):
            return 120

    class Access:
        def click(self, element, *, label):
            clicks.append(label)

    scraper = client.AutoProSaleDetailClient.__new__(client.AutoProSaleDetailClient)
    scraper.folio = "7954"
    scraper._switch_to_wizard = lambda driver: None

    monkeypatch.setattr(client, "assert_no_blocking_prompt", lambda driver: None)
    monkeypatch.setattr(client, "current_tab_name", lambda driver: None)
    monkeypatch.setattr(client, "active_tab_metadata", lambda driver: {})

    def fake_extract_current_tab(driver, *, active_tab=None, include_tables=True):
        step = len(extract_calls)
        extract_calls.append(include_tables)
        tab_name = client.WIZARD_STEP_NAMES[step]
        fields = {
            "Campo Desconocido": "",
            "Paso": tab_name,
        }
        if tab_name == "Forma de Pago":
            fields["Forma Pago"] = "Compra Inteligente"
        if tab_name == "Resumen Venta":
            fields.update(
                {
                    "Comentario": "bono flota\n  devolver completo  ",
                    "Bono Descuento": "$300.000",
                    "% Dcto Recargo": "0",
                    "Dcto Recargo": "$0",
                }
            )
        return {
            "fields": fields,
            "tables": [{"index": 1, "rows": huge_resumen_rows}] if include_tables else [],
            "text": "",
            "_snapshot_meta": {"root_source": "form"},
        }

    monkeypatch.setattr(client, "extract_current_tab", fake_extract_current_tab)
    monkeypatch.setattr(client, "find_safe_next_button", lambda driver: object() if len(clicks) < 7 else None)

    raw = scraper._extract_wizard(object(), Access(), None, Deadline())
    promoted = client.promote_detail(raw)

    assert raw["tab_order"] == list(client.WIZARD_STEP_NAMES)
    assert extract_calls == [False, False, False, False, False, False, False, True]
    assert all(raw["tabs"][name]["tables"] == [] for name in client.WIZARD_STEP_NAMES[:-1])
    assert len(raw["tabs"]["Resumen Venta"]["tables"][0]["rows"]) == 150
    assert raw["tabs"]["Resumen Venta"]["tables"][0]["rows"][-1] == ["item-149", "149"]
    assert raw["tabs"]["Identificación Cliente"]["fields"]["Campo Desconocido"] == ""
    assert promoted["forma_pago"]["fields"]["Forma Pago"] == "Compra Inteligente"
    assert promoted["forma_pago"]["tables"] == []
    assert promoted["comentario"] == "bono flota\n  devolver completo  "
    assert promoted["bono_descuento"] == "$300.000"
    assert promoted["dcto_recargo_pct"] == "0"
    assert promoted["dcto_recargo_amount"] == "$0"
