"""AutoPro credential resolution from widget secret parameters."""

from typing import Any, Tuple

from api.widget_resolver import WidgetParamResolver


class CredentialResolutionError(RuntimeError):
    """Raised when AutoPro credentials cannot be resolved."""


def resolve_autopro_credentials(orchestration_event: Any) -> Tuple[str, str]:
    """Resolve AutoPro username/password from widget params in manifest order."""

    widget_data = (orchestration_event.extra_params or {}).get("widget_data", {})
    resolver = WidgetParamResolver(orchestration_event)
    try:
        username, password = resolver.resolve_positional(widget_data, count=2)
        return _normalize_secret(username), _normalize_secret(password)
    except Exception as exc:
        raise CredentialResolutionError(f"AutoPro widget credentials unavailable: {exc}") from exc


def _normalize_secret(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
