import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


api_module = types.ModuleType("api")
widget_module = types.ModuleType("api.widget_resolver")
files_requests_module = types.ModuleType("api.files_requests")
foundation_module = types.ModuleType("chask_foundation")
foundation_backend_module = types.ModuleType("chask_foundation.backend")
foundation_models_module = types.ModuleType("chask_foundation.backend.models")
foundation_configs_module = types.ModuleType("chask_foundation.configs")
foundation_utils_module = types.ModuleType("chask_foundation.configs.utils")


class _WidgetParamResolver:
    def __init__(self, orchestration_event):
        self.orchestration_event = orchestration_event

    def resolve_positional(self, widget_data, count):
        values = widget_data.get("resolved_values") or widget_data.get("values") or []
        return tuple(values[:count])


class _OrchestrationEvent:
    pass


def _get_secret(*args, **kwargs):
    return '{"BROWSERBASE_API_KEY": "bb-key", "BROWSERBASE_PROJECT_ID": "bb-project"}'


class _FilesApiManager:
    def call(self, name, *args, **kwargs):
        if name == "get_all_files_for_session":
            return {"files": []}
        if name == "upload_file":
            return {"file_uuid": "uploaded-file-uuid"}
        raise AssertionError(name)


widget_module.WidgetParamResolver = _WidgetParamResolver
files_requests_module.files_api_manager = _FilesApiManager()
foundation_models_module.OrchestrationEvent = _OrchestrationEvent
foundation_utils_module.get_secret = _get_secret

sys.modules.setdefault("api", api_module)
sys.modules.setdefault("api.widget_resolver", widget_module)
sys.modules.setdefault("api.files_requests", files_requests_module)
sys.modules.setdefault("chask_foundation", foundation_module)
sys.modules.setdefault("chask_foundation.backend", foundation_backend_module)
sys.modules.setdefault("chask_foundation.backend.models", foundation_models_module)
sys.modules.setdefault("chask_foundation.configs", foundation_configs_module)
sys.modules.setdefault("chask_foundation.configs.utils", foundation_utils_module)
