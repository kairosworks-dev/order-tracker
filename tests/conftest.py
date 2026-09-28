import os

# Keep console exporters quiet in tests; set before app.main is imported.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
