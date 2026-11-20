import logging
import os
import socket
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version

import psutil
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("flight_delay_mlops.telemetry")

tracer = trace.get_tracer("flight_delay_mlops")
meter = metrics.get_meter("flight_delay_mlops")

API_DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1, 2, 5]
_INFERENCE_BUCKET_FACTORS = (0.5, 0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6, 2, 3, 5, 10)

_providers: list[TracerProvider | MeterProvider] = []
_process = psutil.Process()


def inference_buckets(delay_sec: float) -> list[float]:
    base = delay_sec if delay_sec > 0 else 0.01
    return [round(base * factor, 4) for factor in _INFERENCE_BUCKET_FACTORS]


def sdk_disabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "false").strip().lower() == "true"


def _export_enabled(signal: str) -> bool:
    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT")
    )


def _package_version() -> str:
    try:
        return version("flight-delay-mlops")
    except PackageNotFoundError:
        return "unknown"


def build_resource(default_service: str) -> Resource:
    explicit = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
    defaults: dict[str, str] = {
        "service.name": default_service,
        "service.version": _package_version(),
        "service.instance.id": os.getenv("HOSTNAME") or socket.gethostname(),
    }
    if os.getenv("OTEL_SERVICE_NAME"):
        del defaults["service.name"]
    overrides = {
        key: value for key, value in defaults.items() if f"{key}=" not in explicit
    }
    return Resource.create().merge(Resource(overrides))


def _observe_memory(options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(_process.memory_info().rss)


def setup_telemetry(default_service: str) -> None:
    if _providers or sdk_disabled():
        return

    resource = build_resource(default_service)

    tracer_provider = TracerProvider(resource=resource)
    if _export_enabled("TRACES"):
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    readers = []
    if _export_enabled("METRICS"):
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    _providers.extend([tracer_provider, meter_provider])

    AsyncPGInstrumentor().instrument()
    meter.create_observable_gauge(
        "process.memory.rss",
        callbacks=[_observe_memory],
        unit="By",
        description="Resident memory of the process",
    )

    logger.info(
        "telemetry configured",
        extra={
            "otel_service_name": resource.attributes.get("service.name"),
            "otel_instance_id": resource.attributes.get("service.instance.id"),
            "traces_export": _export_enabled("TRACES"),
            "metrics_export": bool(readers),
            "sampler": os.getenv("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
        },
    )


def shutdown_telemetry() -> None:
    while _providers:
        _providers.pop(0).shutdown()
