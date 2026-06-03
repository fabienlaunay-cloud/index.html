"""Structured JSON logging for Railway / Sentry."""
import logging
import json
import time
import os


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        for key in ("user", "job_id", "sku", "duration_ms", "endpoint"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, ensure_ascii=False)


def setup() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "boto3", "botocore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("synqio")


log = setup()
