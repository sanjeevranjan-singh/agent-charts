"""
Flash Agent - ITOps Kubernetes Log Metrics Agent

A lightweight agent that:
1. Collects pod logs from a configured Kubernetes namespace
2. Analyzes logs using an LLM (via LiteLLM proxy) to extract operational metrics
3. Reports traces and metrics to Langfuse for observability
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from kubernetes import client, config
from langfuse import Langfuse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("flash-agent")

# ---------------------------------------------------------------------------
# Configuration (populated from env vars set by the Helm chart)
# ---------------------------------------------------------------------------

AGENT_NAME = os.getenv("AGENT_NAME", "flash-agent")
AGENT_MODE = os.getenv("AGENT_MODE", "active")
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm-proxy.litellm.svc.cluster.local:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "gpt-4o-mini")

# Langfuse (optional – runs fine without it)
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")

# Metrics
TRACE_TAGS = [t.strip() for t in os.getenv("TRACE_TAGS", "flash-agent").split(",") if t.strip()]
LOG_TAIL_LINES = int(os.getenv("LOG_TAIL_LINES", "200"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # seconds between scans (0 = run once)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s – shutting down gracefully", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------------------------


def init_k8s_client() -> client.CoreV1Api:
    """Initialise the Kubernetes API client (in-cluster or kubeconfig)."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded kubeconfig from default location")
    return client.CoreV1Api()


def collect_pod_logs(v1: client.CoreV1Api, namespace: str, tail_lines: int = 200) -> List[Dict[str, Any]]:
    """Collect the last *tail_lines* of logs from every pod in *namespace*."""
    results: List[Dict[str, Any]] = []
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except client.exceptions.ApiException as exc:
        logger.error("Failed to list pods in %s: %s", namespace, exc.reason)
        return results

    for pod in pods.items:
        pod_name = pod.metadata.name
        pod_status = pod.status.phase
        for container in pod.spec.containers:
            try:
                log_text = v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    container=container.name,
                    tail_lines=tail_lines,
                )
            except client.exceptions.ApiException:
                log_text = ""
            results.append({
                "pod": pod_name,
                "container": container.name,
                "namespace": namespace,
                "status": pod_status,
                "logs": log_text or "(no logs)",
            })
    logger.info("Collected logs from %d containers in namespace %s", len(results), namespace)
    return results


def collect_pod_events(v1: client.CoreV1Api, namespace: str) -> List[Dict[str, str]]:
    """Collect recent Kubernetes events for the namespace."""
    events: List[Dict[str, str]] = []
    try:
        event_list = v1.list_namespaced_event(namespace=namespace)
        for ev in event_list.items[-50:]:  # last 50
            events.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message or "",
                "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
                "time": str(ev.last_timestamp or ev.event_time or ""),
            })
    except client.exceptions.ApiException as exc:
        logger.warning("Could not fetch events: %s", exc.reason)
    return events


# ---------------------------------------------------------------------------
# LLM helpers (via LiteLLM proxy)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert IT-Operations analyst. You receive Kubernetes pod logs and events.
Your task:
1. Identify any errors, warnings, anomalies, or performance issues.
2. For each issue found, extract:
   - severity (critical / warning / info)
   - affected_pod
   - affected_container
   - category (one of: CrashLoop, OOM, ImagePull, Connectivity, Latency, ErrorRate, ConfigError, HealthCheck, ResourcePressure, Other)
   - summary (one sentence)
   - recommended_action (one sentence)
3. Produce overall health metrics:
   - total_pods, healthy_pods, unhealthy_pods
   - error_count, warning_count
   - overall_health_score (0-100)

Return ONLY valid JSON with keys: {"issues": [...], "health": {...}}"""


def call_llm(logs_payload: str) -> Optional[Dict[str, Any]]:
    """Send logs to LiteLLM proxy and parse the JSON response."""
    url = f"{LITELLM_URL.rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_MASTER_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

    body = {
        "model": MODEL_ALIAS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": logs_payload},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except requests.exceptions.ConnectionError:
        logger.error("Cannot reach LiteLLM proxy at %s", url)
    except requests.exceptions.HTTPError as exc:
        logger.error("LiteLLM returned HTTP %s: %s", exc.response.status_code, exc.response.text[:500])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.error("Failed to parse LLM response: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Langfuse reporting
# ---------------------------------------------------------------------------


def init_langfuse() -> Optional[Langfuse]:
    """Initialise Langfuse client if credentials are available."""
    if not (LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY):
        logger.info("Langfuse credentials not set – tracing disabled")
        return None
    try:
        lf = Langfuse(
            secret_key=LANGFUSE_SECRET_KEY,
            public_key=LANGFUSE_PUBLIC_KEY,
            host=LANGFUSE_HOST,
        )
        logger.info("Langfuse client initialised (host=%s)", LANGFUSE_HOST)
        return lf
    except Exception as exc:
        logger.warning("Langfuse init failed: %s", exc)
        return None


def report_to_langfuse(
    lf: Langfuse,
    analysis: Dict[str, Any],
    namespace: str,
    duration_sec: float,
    pod_count: int,
):
    """Create a Langfuse trace with the analysis results."""
    trace = lf.trace(
        name=f"{AGENT_NAME}/scan",
        tags=TRACE_TAGS,
        metadata={
            "agent": AGENT_NAME,
            "namespace": namespace,
            "pod_count": pod_count,
            "scan_duration_sec": round(duration_sec, 2),
        },
    )

    health = analysis.get("health", {})
    issues = analysis.get("issues", [])

    # Generation span for the LLM call
    trace.generation(
        name="log-analysis",
        model=MODEL_ALIAS,
        metadata={
            "health_score": health.get("overall_health_score"),
            "error_count": health.get("error_count", 0),
            "warning_count": health.get("warning_count", 0),
            "issue_count": len(issues),
        },
    )

    # Score: overall health
    if "overall_health_score" in health:
        trace.score(
            name="health_score",
            value=health["overall_health_score"] / 100.0,
            comment=f"Namespace {namespace}: {health.get('healthy_pods', '?')}/{health.get('total_pods', '?')} pods healthy",
        )

    # Score: issue count
    trace.score(
        name="issue_count",
        value=float(len(issues)),
        comment=f"{len(issues)} issues detected",
    )

    # Individual issue events
    for idx, issue in enumerate(issues):
        trace.event(
            name=f"issue-{idx}",
            metadata=issue,
        )

    lf.flush()
    logger.info(
        "Langfuse trace created: health_score=%s, issues=%d",
        health.get("overall_health_score", "N/A"),
        len(issues),
    )


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------


def run_scan(v1: client.CoreV1Api, lf: Optional[Langfuse]) -> Dict[str, Any]:
    """Execute a single scan cycle."""
    start = time.time()
    logger.info("=== Scan started for namespace '%s' ===", K8S_NAMESPACE)

    # 1. Collect data
    pod_logs = collect_pod_logs(v1, K8S_NAMESPACE, tail_lines=LOG_TAIL_LINES)
    events = collect_pod_events(v1, K8S_NAMESPACE)

    if not pod_logs:
        logger.warning("No pods found in namespace %s – skipping analysis", K8S_NAMESPACE)
        return {"health": {"overall_health_score": 100, "total_pods": 0}, "issues": []}

    # 2. Build payload for LLM
    payload_parts = [f"=== Kubernetes Namespace: {K8S_NAMESPACE} ===\n"]
    for entry in pod_logs:
        payload_parts.append(
            f"\n--- Pod: {entry['pod']} | Container: {entry['container']} | Status: {entry['status']} ---\n"
            f"{entry['logs'][:4000]}\n"  # truncate per container to stay within context
        )
    if events:
        payload_parts.append("\n=== Recent Events ===\n")
        for ev in events:
            payload_parts.append(f"[{ev['type']}] {ev['reason']}: {ev['message']} ({ev['object']})\n")

    logs_payload = "".join(payload_parts)

    # 3. Analyse with LLM
    logger.info("Sending %d chars to LLM for analysis …", len(logs_payload))
    analysis = call_llm(logs_payload)

    duration = time.time() - start

    if analysis is None:
        logger.error("LLM analysis failed – skipping Langfuse report")
        return {"health": {"overall_health_score": -1}, "issues": []}

    # 4. Report to Langfuse
    if lf:
        report_to_langfuse(lf, analysis, K8S_NAMESPACE, duration, len(pod_logs))

    # 5. Log summary
    health = analysis.get("health", {})
    issues = analysis.get("issues", [])
    logger.info(
        "Scan complete in %.1fs – health_score=%s, issues=%d, pods=%d",
        duration,
        health.get("overall_health_score", "?"),
        len(issues),
        len(pod_logs),
    )
    for issue in issues:
        logger.info(
            "  [%s] %s/%s – %s",
            issue.get("severity", "?").upper(),
            issue.get("affected_pod", "?"),
            issue.get("affected_container", "?"),
            issue.get("summary", ""),
        )

    return analysis


def main():
    logger.info("Flash Agent v1.0.0 starting – agent=%s, namespace=%s, model=%s", AGENT_NAME, K8S_NAMESPACE, MODEL_ALIAS)

    v1 = init_k8s_client()
    lf = init_langfuse()

    if SCAN_INTERVAL <= 0:
        # Run once (CronJob mode)
        run_scan(v1, lf)
    else:
        # Continuous loop (Deployment mode)
        logger.info("Running in continuous mode – scan every %ds", SCAN_INTERVAL)
        while not _shutdown:
            try:
                run_scan(v1, lf)
            except Exception as exc:
                logger.exception("Scan failed: %s", exc)
            # Wait, but check for shutdown every second
            for _ in range(SCAN_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    if lf:
        lf.flush()
    logger.info("Flash Agent shutting down")


if __name__ == "__main__":
    main()
