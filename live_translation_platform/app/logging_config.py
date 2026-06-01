import json
import logging
from datetime import datetime
from typing import Any

from app.middleware import current_request_id
from app.production import sanitize_for_log


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or current_request_id(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = sanitize_for_log(event)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


def configure_logging(settings=None) -> None:
    level_name = getattr(settings, "log_level", "INFO")
    log_format = getattr(settings, "log_format", "text")
    level = getattr(logging, level_name.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(RequestContextFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
