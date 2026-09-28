"""Incident responder: receives Grafana alerts and hands them to a headless coding agent.

POST /alerts on 127.0.0.1:8001. For a firing alert it saves the alert and an
evidence packet (metrics, error logs, error traces) under incidents/<id>/, then
runs Claude Code headless in the repository with the allowlist in
agent-settings.json. One agent run at a time; alerts that arrive meanwhile are
recorded but do not start a second run.

Run from the repository root:  uv run python incident-response/responder.py
"""

import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import uvicorn
from fastapi import FastAPI, Request


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
INCIDENTS = HERE / "incidents"
SETTINGS = HERE / "agent-settings.json"
TASK = HERE / "responder-task.md"

# Read-only queries go through Grafana's data source proxy: one local entry point.
GRAFANA = "http://localhost:3000/api/datasources/proxy/uid"
LOOKBACK_SECONDS = 15 * 60
AGENT_TIMEOUT_SECONDS = 20 * 60
AGENT_BUDGET_USD = "5"

app = FastAPI(title="Incident responder")
agent_lock = threading.Lock()


def sanitize(text):
    """Keep the local home directory out of files that get committed."""
    return text.replace(str(Path.home()), "~")


def write(path, content):
    if not isinstance(content, str):
        content = json.dumps(content, indent=2, default=str)
    path.write_text(sanitize(content) + "\n")


def get_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def query_metrics():
    query = ('sum by (http_route, http_response_status_code) '
             '(increase(http_server_requests_total{job="order-tracker"}[15m]))')
    data = get_json(f"{GRAFANA}/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": query}))
    return {"query": query, "result": [
        {**row["metric"], "requests_last_15m": round(float(row["value"][1]))}
        for row in data["data"]["result"]
    ]}


def query_error_logs(now):
    query = '{service_name="order-tracker"} | severity_text="ERROR"'
    params = urllib.parse.urlencode({
        "query": query, "limit": 10, "direction": "backward",
        "start": (now - LOOKBACK_SECONDS) * 10**9, "end": now * 10**9,
    })
    data = get_json(f"{GRAFANA}/loki/loki/api/v1/query_range?{params}",
                    headers={"X-Loki-Response-Encoding-Flags": "categorize-labels"})
    lines = []
    for stream in data["data"]["result"]:
        for value in stream["values"]:
            metadata = value[2].get("structuredMetadata", {}) if len(value) > 2 else {}
            lines.append({"time_ns": value[0], "line": value[1], **metadata})
    return {"query": query, "lines": lines}


def query_error_traces(now):
    params = urllib.parse.urlencode({
        "q": "{ status = error }", "limit": 3, "start": now - LOOKBACK_SECONDS, "end": now,
    })
    found = get_json(f"{GRAFANA}/tempo/api/search?{params}").get("traces", [])
    traces = []
    for item in found:
        trace = get_json(f"{GRAFANA}/tempo/api/v2/traces/{item['traceID']}")
        spans = []
        for resource in trace["trace"]["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    spans.append({
                        "name": span["name"],
                        "status": span.get("status", {}),
                        "attributes": {a["key"]: next(iter(a["value"].values()), None)
                                       for a in span.get("attributes", [])},
                        "events": [e.get("name") for e in span.get("events", [])],
                    })
        traces.append({"trace_id": item["traceID"], "spans": spans})
    return {"query": "{ status = error }", "traces": traces}


def collect_evidence():
    """Bounded, read-only evidence packet. A failing source is recorded, not fatal."""
    now = int(time.time())
    evidence = {"collected_at": datetime.now(timezone.utc).isoformat(),
                "lookback": f"{LOOKBACK_SECONDS // 60}m"}
    for name, collect in (("metrics", query_metrics),
                          ("error_logs", lambda: query_error_logs(now)),
                          ("error_traces", lambda: query_error_traces(now))):
        try:
            evidence[name] = collect()
        except Exception as exc:
            evidence[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return evidence


def run_agent(incident_dir, alert, evidence):
    prompt = Template(TASK.read_text()).substitute(
        incident_dir=incident_dir.relative_to(REPO),
        alert=json.dumps(alert, indent=2),
        evidence=json.dumps(evidence, indent=2)[:20_000],
    )
    write(incident_dir / "prompt.md", prompt)
    command = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--settings", str(SETTINGS),
        "--setting-sources", "project",   # ignore the user's personal settings
        "--strict-mcp-config",            # and their MCP servers
        "--max-budget-usd", AGENT_BUDGET_USD,
        "--no-session-persistence",
    ]
    started = time.monotonic()
    try:
        done = subprocess.run(command, cwd=REPO, capture_output=True, text=True,
                              timeout=AGENT_TIMEOUT_SECONDS)
        output, exit_code = done.stdout, done.returncode
    except subprocess.TimeoutExpired:
        output, exit_code = "", "timeout"
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        result = {"result": output or "(no output)", "is_error": True}

    write(incident_dir / "response.md", result.get("result", ""))
    write(incident_dir / "run.json", {
        "agent": "claude-code (headless)",
        "models": list(result.get("modelUsage", {}).keys()),
        "exit_code": exit_code,
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "duration_seconds": round(time.monotonic() - started),
        "total_cost_usd": result.get("total_cost_usd"),
        "permission_denials": result.get("permission_denials", []),
    })


def handle(incident_dir, alert):
    try:
        evidence = collect_evidence()
        write(incident_dir / "evidence.json", evidence)
        run_agent(incident_dir, alert, evidence)
    finally:
        agent_lock.release()


@app.post("/alerts", status_code=202)
async def receive_alerts(request: Request):
    payload = await request.json()
    firing = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
    name = (firing or payload.get("alerts") or [{}])[0].get("labels", {}).get("alertname", "alert")
    incident_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    incident_dir = INCIDENTS / incident_id
    incident_dir.mkdir(parents=True)
    write(incident_dir / "alert.json", payload)

    if not firing:
        return {"incident": incident_id, "action": "recorded; nothing is firing"}
    if not agent_lock.acquire(blocking=False):
        return {"incident": incident_id, "action": "recorded; agent already running"}
    threading.Thread(target=handle, args=(incident_dir, payload), daemon=True).start()
    return {"incident": incident_id, "action": "agent started"}


if __name__ == "__main__":
    # Loopback only: nothing outside this machine can start an agent run.
    uvicorn.run(app, host="127.0.0.1", port=8001)
