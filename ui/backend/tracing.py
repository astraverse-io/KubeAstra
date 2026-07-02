"""OpenTelemetry tracing initialization and configuration helper (Phase 5)."""

import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

def init_tracing() -> bool:
    """Initialize OpenTelemetry tracer provider and OTLP exporter if configured.
    
    Returns:
        True if tracing provider was successfully configured, False otherwise.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set. Tracing is disabled (no-op).")
        return False

    try:
        resource = Resource(attributes={
            "service.name": "kubeastra-backend"
        })
        provider = TracerProvider(resource=resource)
        
        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        if protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            
        exporter = OTLPSpanExporter(endpoint=endpoint)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        
        trace.set_tracer_provider(provider)
        logger.info("OpenTelemetry tracing initialized successfully, exporting to: %s via %s", endpoint, protocol)
        return True
    except Exception as exc:
        logger.error("Failed to initialize OpenTelemetry tracing: %s", exc)
        return False

def get_tracer():
    """Retrieve the standard project tracer."""
    return trace.get_tracer("kubeastra")

def current_trace_id() -> str:
    """Resolve the hex-encoded trace ID of the active span, if valid."""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    return f"{ctx.trace_id:032x}" if ctx and ctx.is_valid else ""
