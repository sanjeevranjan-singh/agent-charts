"""
K8s Agent – Kubernetes Fault Detection & Auto-Remediation Agent

A lightweight agent that:
1. Monitors pod health, events, and resource status in a configured namespace
2. Detects faults (CrashLoopBackOff, OOMKilled, ImagePullBackOff, etc.)
3. Analyzes root causes using an LLM (via LiteLLM proxy)
4. Optionally auto-remediates common issues (pod restarts, scaling, etc.)
5. Reports findings and actions to stdout (and optionally Langfuse)
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("k8s-agent")

# ---------------------------------------------------------------------------
# Configuration (populated from env vars set by the Helm chart)
# ---------------------------------------------------------------------------

AGENT_NAME = os.getenv("AGENT_NAME", "k8s-agent")
AGENT_MODE = os.getenv("AGENT_MODE", "active")
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
AUTO_REMEDIATE = os.getenv("AUTO_REMEDIATE", "false").lower() in ("true", "1", "yes")

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm-proxy.litellm.svc.cluster.local:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "gpt-4o-mini")

# Tracing
TRACE_TAGS = [t.strip() for t in os.getenv("TRACE_TAGS", "k8s-agent").split(",") if t.strip()]

# Scan behaviour
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # seconds between scans (0 = run once)
MAX_RESTART_THRESHOLD = int(os.getenv("MAX_RESTART_THRESHOLD", "5"))
LOG_TAIL_LINES = int(os.getenv("LOG_TAIL_LINES", "100"))

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


def init_k8s_clients() -> Tuple[client.CoreV1Api, client.AppsV1Api]:
    """Initialise the Kubernetes API clients (in-cluster or kubeconfig)."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded kubeconfig from default location")
    return client.CoreV1Api(), client.AppsV1Api()


# ---------------------------------------------------------------------------
# Fault Detection
# ---------------------------------------------------------------------------

FAULT_TYPES = {
    "CrashLoopBackOff": "crash_loop",
    "ImagePullBackOff": "image_pull",
    "ErrImagePull": "image_pull",
    "OOMKilled": "oom_killed",
    "CreateContainerConfigError": "config_error",
    "RunContainerError": "runtime_error",
}


def detect_pod_faults(v1: client.CoreV1Api, namespace: str) -> List[Dict[str, Any]]:
    """Scan all pods in the namespace and detect faults."""
    faults: List[Dict[str, Any]] = []
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as exc:
        logger.error("Failed to list pods in %s: %s", namespace, exc.reason)
        return faults

    for pod in pods.items:
        pod_name = pod.metadata.name
        pod_phase = pod.status.phase

        # Check container statuses
        all_statuses = (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])
        for cs in all_statuses:
            container_name = cs.name
            restart_count = cs.restart_count or 0

            # Check waiting state
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                message = cs.state.waiting.message or ""
                fault_type = FAULT_TYPES.get(reason, "unknown_waiting")
                faults.append({
                    "pod": pod_name,
                    "container": container_name,
                    "namespace": namespace,
                    "fault_type": fault_type,
                    "reason": reason,
                    "message": message,
                    "restart_count": restart_count,
                    "phase": pod_phase,
                    "severity": "critical" if reason in ("CrashLoopBackOff", "OOMKilled") else "warning",
                })

            # Check terminated with error
            if cs.state and cs.state.terminated:
                exit_code = cs.state.terminated.exit_code
                reason = cs.state.terminated.reason or ""
                if exit_code != 0 or reason == "OOMKilled":
                    fault_type = FAULT_TYPES.get(reason, "exit_error")
                    faults.append({
                        "pod": pod_name,
                        "container": container_name,
                        "namespace": namespace,
                        "fault_type": fault_type,
                        "reason": reason,
                        "message": f"Exit code: {exit_code}",
                        "restart_count": restart_count,
                        "phase": pod_phase,
                        "severity": "critical" if reason == "OOMKilled" else "warning",
                    })

            # High restart count
            if restart_count >= MAX_RESTART_THRESHOLD and not any(
                f["pod"] == pod_name and f["container"] == container_name for f in faults
            ):
                faults.append({
                    "pod": pod_name,
                    "container": container_name,
                    "namespace": namespace,
                    "fault_type": "high_restarts",
                    "reason": f"Restart count: {restart_count}",
                    "message": f"Container has restarted {restart_count} times (threshold: {MAX_RESTART_THRESHOLD})",
                    "restart_count": restart_count,
                    "phase": pod_phase,
                    "severity": "warning",
                })

        # Pending pods (stuck scheduling)
        if pod_phase == "Pending":
            conditions = pod.status.conditions or []
            for cond in conditions:
                if cond.status == "False":
                    faults.append({
                        "pod": pod_name,
                        "container": "*",
                        "namespace": namespace,
                        "fault_type": "scheduling_failure",
                        "reason": cond.reason or "PodPending",
                        "message": cond.message or "Pod stuck in Pending state",
                        "restart_count": 0,
                        "phase": pod_phase,
                        "severity": "warning",
                    })
                    break

    logger.info("Detected %d fault(s) across pods in namespace %s", len(faults), namespace)
    return faults


def collect_events(v1: client.CoreV1Api, namespace: str) -> List[Dict[str, str]]:
    """Collect recent Warning events from the namespace."""
    events: List[Dict[str, str]] = []
    try:
        event_list = v1.list_namespaced_event(namespace=namespace)
        for ev in event_list.items[-100:]:
            if ev.type == "Warning":
                events.append({
                    "type": ev.type,
                    "reason": ev.reason or "",
                    "message": ev.message or "",
                    "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
                    "count": str(ev.count or 1),
                    "time": str(ev.last_timestamp or ev.event_time or ""),
                })
    except ApiException as exc:
        logger.warning("Could not fetch events: %s", exc.reason)
    return events


def collect_pod_logs_for_faults(
    v1: client.CoreV1Api, faults: List[Dict[str, Any]], tail_lines: int = 100
) -> Dict[str, str]:
    """Collect logs for pods that have faults."""
    logs: Dict[str, str] = {}
    seen_pods = set()
    for fault in faults:
        pod_name = fault["pod"]
        container = fault["container"]
        ns = fault["namespace"]
        key = f"{pod_name}/{container}"
        if key in seen_pods:
            continue
        seen_pods.add(key)
        try:
            kwargs = {"name": pod_name, "namespace": ns, "tail_lines": tail_lines}
            if container != "*":
                kwargs["container"] = container
            log_text = v1.read_namespaced_pod_log(**kwargs)
            logs[key] = log_text[:5000] if log_text else "(empty)"
        except ApiException:
            # Try previous container logs
            try:
                kwargs["previous"] = True
                log_text = v1.read_namespaced_pod_log(**kwargs)
                logs[key] = f"(previous container)\n{log_text[:5000]}" if log_text else "(no logs)"
            except ApiException:
                logs[key] = "(unavailable)"
    return logs


def get_namespace_health(v1: client.CoreV1Api, namespace: str) -> Dict[str, Any]:
    """Get a health summary of all pods in the namespace."""
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except ApiException:
        return {"total_pods": 0, "healthy": 0, "unhealthy": 0, "pending": 0}

    total = len(pods.items)
    healthy = 0
    unhealthy = 0
    pending = 0
    for pod in pods.items:
        if pod.status.phase == "Running":
            # Check if all containers are ready
            all_ready = all(
                (cs.ready for cs in (pod.status.container_statuses or []))
            )
            if all_ready:
                healthy += 1
            else:
                unhealthy += 1
        elif pod.status.phase == "Succeeded":
            healthy += 1
        elif pod.status.phase == "Pending":
            pending += 1
        else:
            unhealthy += 1

    return {
        "total_pods": total,
        "healthy": healthy,
        "unhealthy": unhealthy,
        "pending": pending,
        "health_score": round((healthy / total) * 100, 1) if total > 0 else 100.0,
    }


# ---------------------------------------------------------------------------
# LLM Analysis (via LiteLLM proxy)
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """You are an expert Kubernetes SRE. You receive fault data from a Kubernetes namespace.

For each fault, provide:
1. root_cause: concise root cause analysis
2. recommended_action: specific remediation step
3. auto_remediable: true/false whether this can be safely auto-remediated
4. remediation_type: one of [restart_pod, delete_pod, scale_deployment, rollback_deployment, none]
5. remediation_target: the specific resource name to act on (e.g. deployment name)

Return ONLY valid JSON:
{
  "analysis": [
    {
      "pod": "...",
      "container": "...",
      "fault_type": "...",
      "root_cause": "...",
      "recommended_action": "...",
      "auto_remediable": true/false,
      "remediation_type": "...",
      "remediation_target": "..."
    }
  ],
  "summary": "one-line overall summary"
}"""


def call_llm(payload: str) -> Optional[Dict[str, Any]]:
    """Send fault data to LiteLLM proxy for analysis."""
    url = f"{LITELLM_URL.rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_MASTER_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

    body = {
        "model": MODEL_ALIAS,
        "messages": [
            {"role": "system", "content": ANALYSIS_PROMPT},
            {"role": "user", "content": payload},
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
        logger.warning("Cannot reach LiteLLM proxy at %s – skipping LLM analysis", url)
    except requests.exceptions.HTTPError as exc:
        logger.warning("LiteLLM returned HTTP %s – skipping LLM analysis", exc.response.status_code)
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse LLM response: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Auto-Remediation
# ---------------------------------------------------------------------------


def execute_remediation(
    v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    action: Dict[str, Any],
    namespace: str,
) -> bool:
    """Execute a single remediation action. Returns True on success."""
    rtype = action.get("remediation_type", "none")
    target = action.get("remediation_target", "")
    pod = action.get("pod", "")

    if rtype == "none" or not action.get("auto_remediable", False):
        return False

    try:
        if rtype == "delete_pod" or rtype == "restart_pod":
            logger.info("REMEDIATION: Deleting pod %s/%s to trigger restart", namespace, pod)
            v1.delete_namespaced_pod(name=pod, namespace=namespace)
            logger.info("REMEDIATION: Pod %s deleted successfully", pod)
            return True

        elif rtype == "scale_deployment" and target:
            logger.info("REMEDIATION: Scaling deployment %s/%s to 0 then back to 1", namespace, target)
            # Scale down
            apps_v1.patch_namespaced_deployment_scale(
                name=target,
                namespace=namespace,
                body={"spec": {"replicas": 0}},
            )
            time.sleep(5)
            # Scale up
            apps_v1.patch_namespaced_deployment_scale(
                name=target,
                namespace=namespace,
                body={"spec": {"replicas": 1}},
            )
            logger.info("REMEDIATION: Deployment %s scaled down/up successfully", target)
            return True

        elif rtype == "rollback_deployment" and target:
            logger.info("REMEDIATION: Rolling back deployment %s/%s", namespace, target)
            # Trigger rollout restart (sets annotation to force new rollout)
            now = datetime.now(timezone.utc).isoformat()
            apps_v1.patch_namespaced_deployment(
                name=target,
                namespace=namespace,
                body={
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "k8s-agent.agentcert.io/restarted-at": now,
                                }
                            }
                        }
                    }
                },
            )
            logger.info("REMEDIATION: Deployment %s rollout restart triggered", target)
            return True

    except ApiException as exc:
        logger.error("REMEDIATION FAILED for %s: %s", rtype, exc.reason)
    except Exception as exc:
        logger.error("REMEDIATION FAILED for %s: %s", rtype, exc)

    return False


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------


def run_scan(v1: client.CoreV1Api, apps_v1: client.AppsV1Api) -> Dict[str, Any]:
    """Execute a single scan cycle."""
    start = time.time()
    logger.info("=" * 60)
    logger.info("SCAN START – namespace: %s, auto_remediate: %s", K8S_NAMESPACE, AUTO_REMEDIATE)

    # 1. Get namespace health
    health = get_namespace_health(v1, K8S_NAMESPACE)
    logger.info(
        "Health: %d/%d pods healthy (score: %.1f%%), %d pending",
        health["healthy"], health["total_pods"], health["health_score"], health["pending"],
    )

    # 2. Detect faults
    faults = detect_pod_faults(v1, K8S_NAMESPACE)

    if not faults:
        duration = time.time() - start
        logger.info("No faults detected – namespace is healthy (%.1fs)", duration)
        return {"health": health, "faults": [], "actions": [], "duration": round(duration, 2)}

    # 3. Collect supporting data
    events = collect_events(v1, K8S_NAMESPACE)
    fault_logs = collect_pod_logs_for_faults(v1, faults)

    # 4. Log detected faults
    logger.info("--- Detected Faults ---")
    for f in faults:
        logger.info(
            "  [%s] %s/%s – %s: %s (restarts: %d)",
            f["severity"].upper(),
            f["pod"],
            f["container"],
            f["fault_type"],
            f["reason"],
            f["restart_count"],
        )

    # 5. Attempt LLM analysis
    payload_parts = [
        f"=== Kubernetes Namespace: {K8S_NAMESPACE} ===\n",
        f"Health: {json.dumps(health)}\n\n",
        "=== Detected Faults ===\n",
        json.dumps(faults, indent=2),
        "\n\n=== Warning Events ===\n",
        json.dumps(events[:30], indent=2),
        "\n\n=== Pod Logs (faulted containers) ===\n",
    ]
    for key, log_text in fault_logs.items():
        payload_parts.append(f"\n--- {key} ---\n{log_text}\n")

    payload = "".join(payload_parts)
    logger.info("Sending %d chars to LLM for root-cause analysis…", len(payload))
    llm_analysis = call_llm(payload)

    actions_taken = []

    if llm_analysis:
        summary = llm_analysis.get("summary", "No summary")
        analyses = llm_analysis.get("analysis", [])
        logger.info("LLM Summary: %s", summary)

        # 6. Auto-remediate if enabled
        if AUTO_REMEDIATE and analyses:
            logger.info("--- Auto-Remediation ---")
            for action in analyses:
                if action.get("auto_remediable", False) and action.get("remediation_type", "none") != "none":
                    success = execute_remediation(v1, apps_v1, action, K8S_NAMESPACE)
                    actions_taken.append({
                        "pod": action.get("pod"),
                        "type": action.get("remediation_type"),
                        "target": action.get("remediation_target"),
                        "success": success,
                    })
        elif not AUTO_REMEDIATE and analyses:
            logger.info("Auto-remediation DISABLED – logging recommendations only")
            for action in analyses:
                if action.get("auto_remediable"):
                    logger.info(
                        "  RECOMMENDATION: %s on %s – %s",
                        action.get("remediation_type"),
                        action.get("pod"),
                        action.get("recommended_action"),
                    )
    else:
        logger.info("LLM analysis unavailable – reporting faults without root-cause enrichment")

    duration = time.time() - start
    result = {
        "health": health,
        "faults": faults,
        "llm_analysis": llm_analysis,
        "actions_taken": actions_taken,
        "duration": round(duration, 2),
    }

    logger.info(
        "SCAN COMPLETE in %.1fs – faults: %d, remediations: %d/%d, health_score: %.1f%%",
        duration,
        len(faults),
        sum(1 for a in actions_taken if a["success"]),
        len(actions_taken),
        health["health_score"],
    )
    logger.info("=" * 60)

    return result


def main():
    logger.info(
        "K8s Agent v1.0.0 starting – agent=%s, namespace=%s, model=%s, auto_remediate=%s",
        AGENT_NAME, K8S_NAMESPACE, MODEL_ALIAS, AUTO_REMEDIATE,
    )

    v1, apps_v1 = init_k8s_clients()

    if SCAN_INTERVAL <= 0:
        # Run once (CronJob mode)
        run_scan(v1, apps_v1)
    else:
        # Continuous loop (Deployment mode)
        logger.info("Running in continuous mode – scan every %ds", SCAN_INTERVAL)
        while not _shutdown:
            try:
                run_scan(v1, apps_v1)
            except Exception as exc:
                logger.exception("Scan failed: %s", exc)
            for _ in range(SCAN_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    logger.info("K8s Agent shutting down")


if __name__ == "__main__":
    main()
