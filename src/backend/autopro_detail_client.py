"""Read-only Browserbase/Selenium AutoPro sale-detail client."""

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://autoprocloud.com/mi-cuenta/"
COMPANY_ID = "220"
MODULE_ID = "2"
DEFAULT_BRANCH = "698"
DEFAULT_MAX_SECONDS = 540
DEFAULT_WAIT_SECONDS = 20
PAGE_LOAD_TIMEOUT_SECONDS = 30
SCRIPT_TIMEOUT_SECONDS = 30
GRID_SEARCH_START_DATE = "01-01-2010"

FORBIDDEN_UI_TERMS = ("guardar", "finalizar")
SAFE_NEXT_LABEL = "Siguiente"
CANCEL_LABEL = "Cancelar"


class AutoProReadOnlyViolation(RuntimeError):
    """Raised before any unsafe selector/action can reach Selenium."""


class AutoProDetailUnavailableError(RuntimeError):
    """Raised for portal/network/detail extraction failures."""

    def __init__(self, message: str, *, diagnostics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass
class AutoProSaleDetail:
    folio: str
    branch: str
    browserbase_session_id: str
    detalle_raw: dict[str, Any]
    promoted: dict[str, Any]


class BrowserbaseRemoteConnectionMixin:
    """Factory for Browserbase RemoteConnection with signing-key headers."""

    @staticmethod
    def build(remote_server_addr: str, signing_key: str):
        from selenium.webdriver.remote.remote_connection import RemoteConnection

        class BrowserbaseRemoteConnection(RemoteConnection):
            def __init__(self, addr: str, key: str):
                super().__init__(addr)
                self._signing_key = key

            def get_remote_connection_headers(self, parsed_url, keep_alive=False):
                headers = super().get_remote_connection_headers(parsed_url, keep_alive)
                headers.update({"x-bb-signing-key": self._signing_key})
                return headers

        return BrowserbaseRemoteConnection(remote_server_addr, signing_key)


class ReadOnlyElementAccess:
    """Centralized Selenium lookup/click wrapper that rejects write controls."""

    def __init__(self, driver):
        self.driver = driver

    def find_elements(self, by, selector: str):
        assert_read_only_selector(selector)
        return self.driver.find_elements(by, selector)

    def find_element(self, by, selector: str):
        assert_read_only_selector(selector)
        return self.driver.find_element(by, selector)

    def click(self, element, *, label: str) -> None:
        assert_read_only_selector(label)
        element.click()

    def js_click(self, element, *, label: str) -> None:
        assert_read_only_selector(label)
        self.driver.execute_script("arguments[0].click();", element)


class AutoProSaleDetailClient:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        browserbase_api_key: str,
        browserbase_project_id: str,
        folio: str,
        branch: str = DEFAULT_BRANCH,
        verbose: bool = False,
    ):
        self.username = username
        self.password = password
        self.base_url = base_url or DEFAULT_BASE_URL
        self.browserbase_api_key = browserbase_api_key
        self.browserbase_project_id = browserbase_project_id
        self.folio = normalize_folio(folio)
        self.branch = str(branch or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
        self.verbose = verbose
        self.max_seconds = _positive_int_env("AUTOPRO_DETAIL_MAX_SECONDS", DEFAULT_MAX_SECONDS)
        self._session_id: Optional[str] = None
        self._diagnostics: dict[str, Any] = {}

    def fetch_detail(self) -> AutoProSaleDetail:
        deadline = _OperationDeadline(self.max_seconds)
        session = self._create_browserbase_session()
        deadline.raise_if_expired()
        driver = self._connect_to_session(session)
        self._configure_driver_timeouts(driver)
        access = ReadOnlyElementAccess(driver)
        stop_watchdog = self._start_deadline_watchdog(driver, deadline)
        try:
            wait = _DeadlineAwareWait(driver, deadline)
            self._login(driver, access, wait)
            deadline.raise_if_expired()
            self._select_context(driver, access, wait)
            deadline.raise_if_expired()
            self._open_sales_grid(driver, access, wait)
            deadline.raise_if_expired()
            self._filter_by_folio(driver, access, wait)
            deadline.raise_if_expired()
            self._open_edit_wizard(driver, access, wait)
            deadline.raise_if_expired()
            detalle_raw = self._extract_wizard(driver, access, wait, deadline)
            promoted = promote_detail(detalle_raw)
            return AutoProSaleDetail(
                folio=self.folio,
                branch=self.branch,
                browserbase_session_id=self._session_id or "",
                detalle_raw=detalle_raw,
                promoted=promoted,
            )
        finally:
            stop_watchdog.set()
            try:
                driver.quit()
            except Exception:
                logger.warning("Failed to quit Browserbase driver", exc_info=True)

    def _create_browserbase_session(self):
        from browserbase import Browserbase

        bb = Browserbase(api_key=self.browserbase_api_key)
        session = bb.sessions.create(project_id=self.browserbase_project_id)
        self._session_id = session.id
        self._log("Browserbase session created: %s", session.id)
        return session

    def _connect_to_session(self, session):
        from selenium import webdriver

        custom_conn = BrowserbaseRemoteConnectionMixin.build(
            session.selenium_remote_url,
            session.signing_key,
        )
        options = webdriver.ChromeOptions()
        return webdriver.Remote(custom_conn, options=options)

    @staticmethod
    def _configure_driver_timeouts(driver) -> None:
        try:
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
            driver.set_script_timeout(SCRIPT_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Failed to configure Selenium driver timeouts", exc_info=True)

    @staticmethod
    def _start_deadline_watchdog(driver, deadline: "_OperationDeadline") -> threading.Event:
        stop_event = threading.Event()

        def force_quit_when_expired() -> None:
            if stop_event.wait(deadline.remaining_seconds()):
                return
            logger.error("AutoPro detail operation exceeded %ss deadline; quitting driver", deadline.max_seconds)
            try:
                driver.quit()
            except Exception:
                logger.warning("Failed to quit Selenium driver after deadline", exc_info=True)

        threading.Thread(target=force_quit_when_expired, daemon=True).start()
        return stop_event

    def _login(self, driver, access: ReadOnlyElementAccess, wait) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC

        driver.get(self.base_url)
        iframe = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#Login")))
        driver.switch_to.frame(iframe)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[name*='User']")))

        username_input = self._first_visible(
            access,
            By.CSS_SELECTOR,
            ["input[name*='User']", "input[type='text']", "input[id*='user']", "input[id*='User']"],
        )
        password_input = access.find_element(By.CSS_SELECTOR, "input[type='password']")
        username_input.clear()
        username_input.send_keys(self.username)
        password_input.clear()
        password_input.send_keys(self.password)
        password_input.send_keys(Keys.RETURN)

        wait.until(EC.presence_of_element_located((By.ID, "business")))
        self._wait_for_select_option(access, "business", COMPANY_ID)
        self._log("AutoPro login complete")

    def _select_context(self, driver, access: ReadOnlyElementAccess, wait) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import Select

        self._wait_for_select_option(access, "business", COMPANY_ID)
        Select(access.find_element(By.ID, "business")).select_by_value(COMPANY_ID)
        self._wait_for_select_option(access, "branch", self.branch)
        Select(access.find_element(By.ID, "branch")).select_by_value(self.branch)
        self._wait_for_select_option(access, "module", MODULE_ID)
        Select(access.find_element(By.ID, "module")).select_by_value(MODULE_ID)
        self._diagnostics["selected_context"] = selected_context_state(driver)
        access.click(wait.until(EC.element_to_be_clickable((By.ID, "LoginOk"))), label="LoginOk")
        driver.switch_to.default_content()
        time.sleep(8)
        wait.until(EC.presence_of_element_located((By.LINK_TEXT, "Operaciones")))
        self._log("AutoPro context selected: company=%s branch=%s module=%s", COMPANY_ID, self.branch, MODULE_ID)

    def _open_sales_grid(self, driver, access: ReadOnlyElementAccess, wait) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC

        driver.switch_to.default_content()
        link = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//a[contains(@href, 'showdms_venta_vehiculotable')]")
            )
        )
        access.js_click(link, label="menu venta vehiculos")
        time.sleep(6)
        self._switch_to_grid(driver)
        self._log("Opened Consulta Venta Vehiculo via menu")

    def _filter_by_folio(self, driver, access: ReadOnlyElementAccess, wait) -> None:
        self._switch_to_grid(driver)
        diagnostics = dict(self._diagnostics)
        diagnostics["filters_before"] = visible_filter_control_metadata(driver)
        diagnostics["applied_filters"] = apply_grid_filters_for_folio(access, self.folio)
        diagnostics["filters_after"] = visible_filter_control_metadata(driver)
        search = find_grid_search_button(access)
        access.click(search, label="Buscar")
        time.sleep(3)
        diagnostics["post_search"] = grid_result_markers(driver, self.folio)
        if not page_contains_text(driver, self.folio):
            raise AutoProDetailUnavailableError(
                f"Folio {self.folio} not found in AutoPro grid after filtering",
                diagnostics=diagnostics,
            )
        self._log("Folio filter applied: %s", self.folio)

    def _open_edit_wizard(self, driver, access: ReadOnlyElementAccess, wait) -> None:
        from selenium.webdriver.common.by import By

        self._switch_to_grid(driver)
        row = self._find_row_containing(driver, self.folio)
        if row is None:
            raise AutoProDetailUnavailableError(f"Folio {self.folio} row not found")
        edit = first_existing_within(
            row,
            By.XPATH,
            [
                ".//a[contains(@href, 'showdms_venta_vehiculo') and contains(@href, 'edit')]",
                ".//a[contains(@title, 'Editar') or contains(@aria-label, 'Editar')]",
                ".//input[contains(@title, 'Editar') or contains(@aria-label, 'Editar')]",
                ".//a[contains(@class, 'edit') or contains(@class, 'Edit')]",
            ],
        )
        if edit is None:
            raise AutoProDetailUnavailableError(f"Folio {self.folio} edit link not found")
        access.js_click(edit, label="Editar Venta Vehiculo")
        time.sleep(5)
        self._switch_to_wizard(driver)
        assert_no_blocking_prompt(driver)
        self._log("Opened read-only wizard for folio %s", self.folio)

    def _extract_wizard(self, driver, access: ReadOnlyElementAccess, wait, deadline: "_OperationDeadline") -> dict[str, Any]:
        tabs: dict[str, Any] = {}
        visited = set()
        safety_steps = 0

        while safety_steps < 20:
            deadline.raise_if_expired()
            self._switch_to_wizard(driver)
            assert_no_blocking_prompt(driver)
            tab_name = current_tab_name(driver) or f"tab_{safety_steps + 1}"
            normalized = normalize_key(tab_name)
            if normalized not in visited:
                tabs[tab_name] = extract_current_tab(driver)
                visited.add(normalized)

            next_button = find_safe_next_button(driver)
            if next_button is None:
                break
            access.click(next_button, label=SAFE_NEXT_LABEL)
            time.sleep(1.5)
            safety_steps += 1

        return {
            "folio": self.folio,
            "tabs": tabs,
            "tab_order": list(tabs.keys()),
        }

    def _switch_to_grid(self, driver, timeout: float = 25.0) -> None:
        from selenium.webdriver.common.by import By

        end = time.time() + timeout
        while time.time() < end:
            driver.switch_to.default_content()
            if self._grid_ready(driver):
                return
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                driver.switch_to.default_content()
                try:
                    driver.switch_to.frame(frame)
                except Exception:
                    continue
                if self._grid_ready(driver):
                    return
            time.sleep(1)
        driver.switch_to.default_content()
        raise AutoProDetailUnavailableError("AutoPro ventas grid not found after menu navigation")

    def _switch_to_wizard(self, driver, timeout: float = 25.0) -> None:
        from selenium.webdriver.common.by import By

        end = time.time() + timeout
        while time.time() < end:
            driver.switch_to.default_content()
            if wizard_ready(driver):
                return
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                driver.switch_to.default_content()
                try:
                    driver.switch_to.frame(frame)
                except Exception:
                    continue
                if wizard_ready(driver):
                    return
            time.sleep(1)
        driver.switch_to.default_content()
        raise AutoProDetailUnavailableError("AutoPro Editar Venta Vehiculo wizard not found")

    @staticmethod
    def _grid_ready(driver) -> bool:
        from selenium.webdriver.common.by import By

        return bool(driver.find_elements(By.ID, "ctl00_PageContent_Dms_Venta_VehiculoFilterButton__Button"))

    @staticmethod
    def _wait_for_select_option(access: ReadOnlyElementAccess, select_id: str, value: str, timeout: float = 20.0) -> None:
        from selenium.webdriver.common.by import By

        selector = f"#{select_id} option[value='{value}']"
        end = time.time() + timeout
        while time.time() < end:
            if access.find_elements(By.CSS_SELECTOR, selector):
                return
            time.sleep(0.25)
        raise AutoProDetailUnavailableError(f"AutoPro select #{select_id} never populated option value={value}")

    @staticmethod
    def _first_visible(access: ReadOnlyElementAccess, by, selectors):
        for selector in selectors:
            elements = access.find_elements(by, selector)
            for element in elements:
                if element.is_displayed():
                    return element
        raise AutoProDetailUnavailableError("Could not find visible AutoPro input")

    @staticmethod
    def _first_present(access: ReadOnlyElementAccess, by, selectors):
        for selector in selectors:
            elements = access.find_elements(by, selector)
            if elements:
                return elements[0]
        raise AutoProDetailUnavailableError(f"Could not find any of selectors: {selectors}")

    @staticmethod
    def _find_row_containing(driver, text: str):
        from selenium.webdriver.common.by import By

        rows = driver.find_elements(By.XPATH, f"//tr[.//*[contains(normalize-space(.), {xpath_literal(text)})] or contains(normalize-space(.), {xpath_literal(text)})]")
        return rows[0] if rows else None

    def _log(self, message: str, *args) -> None:
        if self.verbose:
            logger.info(message, *args)


def assert_read_only_selector(value: str) -> None:
    normalized = normalize_text(value)
    for term in FORBIDDEN_UI_TERMS:
        if term in normalized:
            raise AutoProReadOnlyViolation(f"Refusing unsafe AutoPro selector/action: {redact_selector(value)}")


def assert_no_blocking_prompt(driver) -> None:
    from selenium.webdriver.common.by import By

    prompt_selectors = [
        ".modal.show",
        ".bootbox.modal",
        "[role='dialog']",
        ".ui-dialog",
        ".swal2-container",
    ]
    for selector in prompt_selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element.is_displayed():
                text = normalize_space(element.text)
                raise AutoProDetailUnavailableError(f"AutoPro blocking prompt appeared; read-only extraction aborted: {text[:160]}")


def find_safe_next_button(driver):
    from selenium.webdriver.common.by import By

    candidates = driver.find_elements(
        By.XPATH,
        (
            "//*[self::a or self::button or self::input]"
            "[not(@disabled)]"
            "[contains(normalize-space(.), 'Siguiente')"
            " or @value='Siguiente'"
            " or contains(@title, 'Siguiente')"
            " or contains(@aria-label, 'Siguiente')]"
        ),
    )
    visible = [candidate for candidate in candidates if candidate.is_displayed()]
    return visible[0] if visible else None


def find_grid_search_button(access: ReadOnlyElementAccess):
    from selenium.webdriver.common.by import By

    exact_id = "ctl00_PageContent_Dms_Venta_VehiculoFilterButton__Button"
    try:
        return access.find_element(By.ID, exact_id)
    except Exception:
        return AutoProSaleDetailClient._first_present(
            access,
            By.CSS_SELECTOR,
            [
                "input[id$='FilterButton__Button']",
                "button[id$='FilterButton__Button']",
                "input[type='submit'][value*='Buscar']",
                "button[type='submit']",
            ],
        )


def apply_grid_filters_for_folio(access: ReadOnlyElementAccess, folio: str) -> dict[str, Any]:
    from selenium.webdriver.common.by import By

    applied: dict[str, Any] = {"date_filters": [], "folio_filter": None}
    date_to = date.today().strftime("%d-%m-%Y")
    for selector, value in [
        ("ctl00_PageContent_FechaFromFilter", GRID_SEARCH_START_DATE),
        ("ctl00_PageContent_FechaToFilter", date_to),
    ]:
        elements = access.find_elements(By.ID, selector)
        if elements:
            elements[0].clear()
            elements[0].send_keys(value)
            applied["date_filters"].append({"id": selector, "value_set": value})

    folio_input = AutoProSaleDetailClient._first_present(
        access,
        By.CSS_SELECTOR,
        [
            "input[id*='Folio'][id*='Filter']",
            "input[name*='Folio'][name*='Filter']",
            "input[id*='Numero'][id*='Filter']",
            "input[name*='Numero'][name*='Filter']",
        ],
    )
    folio_input.clear()
    folio_input.send_keys(folio)
    applied["folio_filter"] = {
        "target": element_metadata(folio_input, include_value=True),
        "value_set": folio,
    }
    return applied


def selected_context_state(driver) -> dict[str, Any]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    state: dict[str, Any] = {}
    for select_id in ["business", "branch", "module"]:
        elements = driver.find_elements(By.ID, select_id)
        if not elements:
            state[select_id] = {"present": False}
            continue
        element = elements[0]
        selected_value = element.get_attribute("value") or ""
        selected_text = ""
        try:
            selected_text = normalize_space(Select(element).first_selected_option.text)
        except Exception:
            selected_text = ""
        state[select_id] = {
            "present": True,
            "value": selected_value,
            "selected_text": selected_text,
        }
    return state


def visible_filter_control_metadata(driver) -> list[dict[str, Any]]:
    from selenium.webdriver.common.by import By

    controls = []
    for element in driver.find_elements(By.CSS_SELECTOR, "input[id*='Filter'], input[name*='Filter'], select[id*='Filter'], select[name*='Filter'], textarea[id*='Filter'], textarea[name*='Filter']"):
        try:
            if element.is_displayed():
                metadata = element_metadata(element, include_value=True)
                if metadata:
                    controls.append(metadata)
        except Exception:
            continue
    return controls[:80]


def element_metadata(element, *, include_value: bool) -> dict[str, Any]:
    tag = (element.tag_name or "").lower()
    control_type = (element.get_attribute("type") or "").lower()
    if control_type == "password":
        return {}
    metadata = {
        "tag": tag,
        "id": element.get_attribute("id") or "",
        "name": element.get_attribute("name") or "",
        "type": control_type,
        "placeholder": element.get_attribute("placeholder") or "",
        "title": element.get_attribute("title") or "",
        "aria_label": element.get_attribute("aria-label") or "",
    }
    if include_value:
        metadata["value"] = safe_control_value(element, metadata)
    return metadata


def safe_control_value(element, metadata: dict[str, Any]) -> str:
    haystack = normalize_key(
        " ".join(
            [
                metadata.get("id", ""),
                metadata.get("name", ""),
                metadata.get("placeholder", ""),
                metadata.get("title", ""),
                metadata.get("aria_label", ""),
            ]
        )
    )
    if any(term in haystack for term in ["cliente", "rut", "nombre", "apellido", "email", "correo", "telefono"]):
        return "[redacted-if-present]" if element.get_attribute("value") else ""
    return element.get_attribute("value") or ""


def grid_result_markers(driver, folio: str) -> dict[str, Any]:
    from selenium.webdriver.common.by import By

    markers: dict[str, Any] = {
        "folio_visible": page_contains_text(driver, folio),
        "empty_state": empty_grid_marker(driver),
        "safe_rows": [],
    }
    rows = driver.find_elements(By.XPATH, "//table//tr")
    for index, row in enumerate(rows[:50], start=1):
        cells = [normalize_space(cell.text) for cell in row.find_elements(By.XPATH, "./th|./td")]
        if not any(cells):
            continue
        safe_cells = safe_row_identifiers(cells, folio)
        if safe_cells:
            markers["safe_rows"].append({"row_index": index, "identifiers": safe_cells})
    return markers


def safe_row_identifiers(cells: list[str], folio: str) -> list[str]:
    safe = []
    for cell in cells:
        if not cell:
            continue
        if folio in cell:
            safe.append(cell[:80])
            continue
        normalized = normalize_text(cell)
        if normalized in {"editar", "ver", "seleccionar", "modificar"}:
            safe.append(cell[:40])
    return safe[:6]


def empty_grid_marker(driver) -> Optional[str]:
    text = normalize_text(visible_body_text(driver))
    for marker in [
        "no se encontraron",
        "sin registros",
        "no existen registros",
        "no hay registros",
        "no records",
    ]:
        if marker in text:
            return marker
    return None


def wizard_ready(driver) -> bool:
    from selenium.webdriver.common.by import By

    return bool(driver.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'Identificacion Cliente') or contains(normalize-space(.), 'Resumen Venta') or contains(normalize-space(.), 'Datos Vehiculo')]"))


def current_tab_name(driver) -> Optional[str]:
    from selenium.webdriver.common.by import By

    selectors = [
        "//*[contains(@class, 'active') and (self::li or self::a or self::span)][normalize-space(.)!='']",
        "//h1[normalize-space(.)!='']",
        "//h2[normalize-space(.)!='']",
        "//legend[normalize-space(.)!='']",
    ]
    for selector in selectors:
        for element in driver.find_elements(By.XPATH, selector):
            text = normalize_space(element.text)
            if text and len(text) <= 80:
                return text
    return None


def extract_current_tab(driver) -> dict[str, Any]:
    return {
        "fields": extract_label_value_fields(driver),
        "tables": extract_tables(driver),
        "text": visible_body_text(driver),
    }


def extract_label_value_fields(driver) -> dict[str, Any]:
    from selenium.webdriver.common.by import By

    fields: dict[str, Any] = {}
    controls = driver.find_elements(By.XPATH, "//input|//select|//textarea")
    for control in controls:
        if not control.is_displayed():
            continue
        label = label_for_control(driver, control)
        value = control_value(control)
        if label:
            add_multivalue(fields, label, value)

    definition_rows = driver.find_elements(By.XPATH, "//dt[normalize-space(.)!='' and following-sibling::dd[1]]")
    for dt in definition_rows:
        label = normalize_space(dt.text)
        try:
            value = normalize_space(dt.find_element(By.XPATH, "following-sibling::dd[1]").text)
        except Exception:
            value = ""
        if label and value:
            add_multivalue(fields, label, value)
    return fields


def label_for_control(driver, control) -> str:
    from selenium.webdriver.common.by import By

    control_id = control.get_attribute("id") or ""
    if control_id:
        labels = driver.find_elements(By.XPATH, f"//label[@for={xpath_literal(control_id)}]")
        for label in labels:
            text = normalize_space(label.text)
            if text:
                return text
    for selector in [
        "ancestor::div[contains(@class, 'form-group')][1]//label[normalize-space(.)!='']",
        "ancestor::td[1]/preceding-sibling::td[1]",
        "preceding::label[normalize-space(.)!=''][1]",
    ]:
        try:
            text = normalize_space(control.find_element(By.XPATH, selector).text)
            if text:
                return text
        except Exception:
            continue
    name = control.get_attribute("name") or control_id
    return normalize_space(name)


def control_value(control) -> str:
    tag = (control.tag_name or "").lower()
    if tag == "select":
        try:
            selected = control.find_element("xpath", ".//option[@selected]")
            text = normalize_space(selected.text)
            if text:
                return text
        except Exception:
            pass
    value = control.get_attribute("value")
    if value is None:
        value = control.text
    return normalize_space(value)


def extract_tables(driver) -> list[dict[str, Any]]:
    from selenium.webdriver.common.by import By

    tables = []
    for index, table in enumerate(driver.find_elements(By.XPATH, "//table"), start=1):
        if not table.is_displayed():
            continue
        rows = []
        for row in table.find_elements(By.XPATH, ".//tr"):
            cells = [normalize_space(cell.text) for cell in row.find_elements(By.XPATH, "./th|./td")]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append({"index": index, "rows": rows})
    return tables


def visible_body_text(driver) -> str:
    from selenium.webdriver.common.by import By

    try:
        return normalize_space(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        return ""


def promote_detail(detalle_raw: dict[str, Any]) -> dict[str, Any]:
    tabs = detalle_raw.get("tabs") or {}
    all_fields = flatten_fields(tabs)
    forma_pago_tab = first_tab_matching(tabs, "forma de pago")
    resumen_tab = first_tab_matching(tabs, "resumen")
    promoted = {
        "comentario": first_value_by_label(all_fields, ["comentario", "observacion", "observaciones"]),
        "numero_chasis": first_value_by_label(all_fields, ["chasis", "numero chasis", "número chasis", "nro chasis"]),
        "vin_or_unidad_id": first_value_by_label(all_fields, ["vin", "numero vin", "número vin", "codigo interno", "código interno", "chasis", "numero chasis", "número chasis"]),
        "uso_vehiculo": first_value_by_label(all_fields, ["uso vehiculo", "uso del vehiculo", "uso"]),
        "tipo_venta_detalle": first_value_by_label(all_fields, ["tipo venta", "tipo de venta", "tipo venta detalle"]),
        "forma_pago": forma_pago_payload(forma_pago_tab),
        "bono_descuento": first_value_by_label(all_fields, ["bono descuento", "bono", "descuento bono"]),
        "dcto_recargo_pct": first_value_by_label(all_fields, ["dcto recargo pct", "% dcto recargo", "descuento recargo %", "dcto/recargo %"]),
        "dcto_recargo_amount": first_value_by_label(all_fields, ["dcto recargo", "descuento recargo", "dcto/recargo", "monto descuento"]),
        "total_vehiculo_cliente": first_value_by_label(all_fields, ["total vehiculo cliente", "total vehiculo", "total cliente"]),
        "fecha_entrega": first_value_by_label(all_fields, ["fecha entrega", "fecha de entrega"]),
    }
    if resumen_tab:
        promoted["resumen_venta"] = {
            "fields": resumen_tab.get("fields", {}),
            "tables": resumen_tab.get("tables", []),
        }
    return promoted


def flatten_fields(tabs: dict[str, Any]) -> dict[str, list[str]]:
    flattened: dict[str, list[str]] = {}
    for tab in tabs.values():
        fields = tab.get("fields") if isinstance(tab, dict) else {}
        if not isinstance(fields, dict):
            continue
        for label, value in fields.items():
            values = value if isinstance(value, list) else [value]
            flattened.setdefault(normalize_key(label), []).extend(str(item) for item in values if item not in (None, ""))
    return flattened


def first_tab_matching(tabs: dict[str, Any], needle: str) -> Optional[dict[str, Any]]:
    normalized_needle = normalize_key(needle)
    for name, tab in tabs.items():
        if normalized_needle in normalize_key(name) and isinstance(tab, dict):
            return tab
    return None


def first_value_by_label(fields: dict[str, list[str]], labels: list[str]) -> Optional[str]:
    for label in labels:
        values = fields.get(normalize_key(label))
        if values:
            return values[0]
    for label in labels:
        needle = normalize_key(label)
        for key, values in fields.items():
            if needle in key and values:
                return values[0]
    return None


def forma_pago_payload(tab: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not tab:
        return None
    return {
        "fields": tab.get("fields", {}),
        "tables": tab.get("tables", []),
        "text": tab.get("text", ""),
    }


def add_multivalue(target: dict[str, Any], label: str, value: str) -> None:
    if label not in target:
        target[label] = value
        return
    existing = target[label]
    if isinstance(existing, list):
        existing.append(value)
    else:
        target[label] = [existing, value]


def page_contains_text(driver, value: str) -> bool:
    return normalize_text(value) in normalize_text(visible_body_text(driver))


def first_existing_within(root, by, selectors: list[str]):
    for selector in selectors:
        assert_read_only_selector(selector)
        elements = root.find_elements(by, selector)
        if elements:
            return elements[0]
    return None


def normalize_folio(value: Any) -> str:
    folio = str(value or "").strip()
    if not folio:
        raise ValueError("Missing required parameter: folio")
    if not re.fullmatch(r"\d+", folio):
        raise ValueError("folio must be numeric")
    return folio


def normalize_key(value: Any) -> str:
    return normalize_text(value).replace(":", "").strip()


def normalize_text(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return normalize_space(text).casefold()


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def redact_selector(value: str) -> str:
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."


class _OperationDeadline:
    def __init__(self, max_seconds: int):
        self.max_seconds = max_seconds
        self._expires_at = time.monotonic() + max_seconds

    def remaining_seconds(self) -> float:
        return max(self._expires_at - time.monotonic(), 0.0)

    def raise_if_expired(self) -> None:
        if self.remaining_seconds() <= 0:
            raise AutoProDetailUnavailableError(f"AutoPro detail operation exceeded {self.max_seconds}s deadline")


class _DeadlineAwareWait:
    def __init__(self, driver, deadline: _OperationDeadline):
        self.driver = driver
        self.deadline = deadline

    def until(self, method, message: str = ""):
        from selenium.webdriver.support.ui import WebDriverWait

        self.deadline.raise_if_expired()
        seconds = min(DEFAULT_WAIT_SECONDS, self.deadline.remaining_seconds())
        if seconds <= 0:
            self.deadline.raise_if_expired()
        return WebDriverWait(self.driver, seconds).until(method, message)


def _positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, value, default)
        return default
    if parsed <= 0:
        logger.warning("Ignoring non-positive %s=%r; using %s", name, value, default)
        return default
    return parsed
