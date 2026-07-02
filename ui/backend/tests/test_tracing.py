"""Unit and integration tests for OpenTelemetry tracing and thread context propagation (Phase 5)."""

import os
import sys
import json
import pytest

# Ensure backend directory is in sys.path
from pathlib import Path
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Add the mcp directory to path too
MCP_DIR = BACKEND_DIR.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

@pytest.fixture(autouse=True)
def _scope_otel_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import tracing


def test_init_tracing_degrades_gracefully_when_no_endpoint(monkeypatch):
    """Verify that tracing initialization does not crash when the OTLP endpoint is missing."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # This should complete without exceptions
    tracing.init_tracing()


def test_tracing_and_thread_context_propagation(monkeypatch):
    """Verify that all manual spans are nested correctly and attributes are captured."""
    # Set up in-memory exporter to capture spans
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    
    # Set the global tracer provider for this test run
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(tracing, "get_tracer", lambda: provider.get_tracer("kubeastra"))
    
    import react
    from services.llm.pricing import TokenUsage
    
    # Mock LLM provider
    class DummyProvider:
        model = "gemini-2.5-flash"
        def generate_stream_with_usage(self, prompt, system=None, temperature=0.2, max_tokens=None):
            usage = TokenUsage(tokens_in=10, cached_tokens_in=0, tokens_out=5, model="gemini-2.5-flash", cost_usd=0.001)
            def gen():
                yield '{"thought": "calling tool", "action": "investigate_pod", "params": {}}'
            return gen(), [usage]

        def generate_stream(self, prompt, system=None, temperature=0.2, max_tokens=None):
            yield "this is the final answer"
            
    # Mock dispatch
    def dummy_dispatch(tool, params):
        return {"status": "success", "result": "done"}
        
    # Start a parent request span (simulating a FastAPI HTTP request span)
    tracer = tracing.get_tracer()
    with tracer.start_as_current_span("http_request") as parent_span:
        # Run ReAct loop
        react.react_loop(
            question="what is happening?",
            history=[],
            provider=DummyProvider(),
            dispatch_fn=dummy_dispatch,
            max_iterations=1,
        )
        
    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    
    assert "react_loop" in span_names
    assert "react_iteration" in span_names
    assert "llm_call" in span_names
    assert "tool_dispatch" in span_names
    assert "http_request" in span_names
    
    # Find individual spans
    http_span = next(s for s in spans if s.name == "http_request")
    react_loop_span = next(s for s in spans if s.name == "react_loop")
    react_iter_span = next(s for s in spans if s.name == "react_iteration")
    llm_span = next(s for s in spans if s.name == "llm_call")
    tool_span = next(s for s in spans if s.name == "tool_dispatch")
    
    # Verify nesting: react_loop must be a child of http_request
    assert react_loop_span.parent.span_id == http_span.context.span_id
    
    # Verify nesting of iterations under react_loop
    assert react_iter_span.parent.span_id == react_loop_span.context.span_id
    
    # Verify nesting of LLM calls under iteration
    assert llm_span.parent.span_id == react_iter_span.context.span_id
    
    # Verify nesting of tool dispatch under iteration
    assert tool_span.parent.span_id == react_iter_span.context.span_id
    
    # Verify attributes
    assert react_iter_span.attributes["iteration_number"] == 1
    assert tool_span.attributes["tool_name"] == "investigate_pod"
    assert tool_span.attributes["status"] == "success"
    assert llm_span.attributes["model"] == "gemini-2.5-flash"
    assert llm_span.attributes["tokens_in"] == 10
    assert llm_span.attributes["tokens_out"] == 5


def test_provider_wrapping_is_not_mutated_and_non_multiplying(monkeypatch):
    """Verify that the provider's methods are not permanently mutated, preventing multiplying spans on subsequent calls."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(tracing, "get_tracer", lambda: provider.get_tracer("kubeastra"))
    
    import react
    from services.llm.pricing import TokenUsage
    
    class MockProvider:
        model = "gemini-2.5-flash"
        def generate_stream_with_usage(self, prompt, system=None, temperature=0.2, max_tokens=None):
            usage = TokenUsage(tokens_in=10, cached_tokens_in=0, tokens_out=5, model="gemini-2.5-flash", cost_usd=0.001)
            def gen():
                yield '{"thought": "calling tool", "action": "investigate_pod", "params": {}}'
            return gen(), [usage]

        def generate_stream(self, prompt, system=None, temperature=0.2, max_tokens=None):
            yield "this is the final answer"
            
    def dummy_dispatch(tool, params):
        return {"status": "success", "result": "done"}
        
    p = MockProvider()
    
    # Check that methods are not wrapped initially
    assert not hasattr(p.generate_stream_with_usage, "__closure__") or p.generate_stream_with_usage.__name__ == "generate_stream_with_usage"

    # Call 1
    react.react_loop(
        question="run 1",
        history=[],
        provider=p,
        dispatch_fn=dummy_dispatch,
        max_iterations=1,
    )
    
    # Check that the provider methods were NOT mutated permanently
    assert not hasattr(p.generate_stream_with_usage, "__closure__") or p.generate_stream_with_usage.__name__ == "generate_stream_with_usage"
    
    spans_run1 = exporter.get_finished_spans()
    llm_spans_run1 = [s for s in spans_run1 if s.name == "llm_call"]
    # 3 spans expected: 1 for react turn, 1 for critic, 1 for action reviewer
    assert len(llm_spans_run1) == 3

    exporter.clear()

    # Call 2
    react.react_loop(
        question="run 2",
        history=[],
        provider=p,
        dispatch_fn=dummy_dispatch,
        max_iterations=1,
    )
    
    # Check again
    assert not hasattr(p.generate_stream_with_usage, "__closure__") or p.generate_stream_with_usage.__name__ == "generate_stream_with_usage"
    
    spans_run2 = exporter.get_finished_spans()
    llm_spans_run2 = [s for s in spans_run2 if s.name == "llm_call"]
    # Still exactly 3 spans expected, confirming no nested wraps occurred
    assert len(llm_spans_run2) == 3
