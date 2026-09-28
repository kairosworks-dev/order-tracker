"""OpenTelemetry setup and request instrumentation.

Signals are printed to stdout for now, so they show up in `docker compose logs app`.
"""

import logging
from contextlib import contextmanager

from fastapi import HTTPException
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode


SERVICE_NAME = "order-tracker"

# The API hands out proxies until setup() installs the SDK providers behind them.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
logger = logging.getLogger("order_tracker")

requests_counter = meter.create_counter(
    "http.server.requests",
    unit="{request}",
    description="HTTP requests by route and response status code",
)


def setup():
    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Export interval comes from OTEL_METRIC_EXPORT_INTERVAL (default 60 s).
    reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogRecordExporter()))
    set_logger_provider(logger_provider)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    logger.setLevel(logging.INFO)


@contextmanager
def track_request(route, method="GET", attributes=None):
    """Trace, count and log one request, including requests that crash.

    `route` is the path template, never the concrete path: metric labels must
    stay low-cardinality, so per-request values like the order ID go on the
    span and the log instead.
    """
    status = 200
    labels = {"http.route": route, "http.request.method": method}
    with tracer.start_as_current_span(
        f"{method} {route}",
        kind=trace.SpanKind.SERVER,
        attributes={**labels, **(attributes or {})},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except HTTPException as exc:
            status = exc.status_code
            raise
        except Exception as exc:
            status = 500
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            logger.exception("%s %s failed", method, route,
                             extra={**labels, **(attributes or {}), "http.response.status_code": status})
            raise
        finally:
            labels["http.response.status_code"] = status
            span.set_attribute("http.response.status_code", status)
            requests_counter.add(1, labels)
            if status < 500:
                logger.info("%s %s -> %d", method, route, status,
                            extra={**labels, **(attributes or {})})
