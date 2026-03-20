#!/usr/bin/env bash
set -euo pipefail

# Builds install-agent image locally and removes older local image tags.
# Usage:
#   ./local_custom/build-install-agent-local.sh
# Optional env overrides:
#   IMAGE_REPO=myuser/agentcert-install-agent
#   IMAGE_TAG=local-20260320-120000
#   KEEP_LATEST_ALIAS=true|false
#   CLEAN_DANGLING=true|false
#   LOAD_TO_MINIKUBE=true|false
#   CLEAN_MINIKUBE_OLD=true|false
#   MINIKUBE_PROFILE=minikube

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_REPO="${IMAGE_REPO:-agentcert/agentcert-install-agent}"
IMAGE_TAG="${IMAGE_TAG:-local-$(date +%Y%m%d%H%M%S)}"
KEEP_LATEST_ALIAS="${KEEP_LATEST_ALIAS:-true}"
CLEAN_DANGLING="${CLEAN_DANGLING:-false}"
LOAD_TO_MINIKUBE="${LOAD_TO_MINIKUBE:-true}"
CLEAN_MINIKUBE_OLD="${CLEAN_MINIKUBE_OLD:-true}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-minikube}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not in PATH"
  exit 1
fi

echo "Repo root: ${REPO_ROOT}"
echo "Building: ${IMAGE_REPO}:${IMAGE_TAG}"

cd "${REPO_ROOT}"
docker build --no-cache -t "${IMAGE_REPO}:${IMAGE_TAG}" -f install-agent/Dockerfile .

if [[ "${KEEP_LATEST_ALIAS}" == "true" ]]; then
  docker tag "${IMAGE_REPO}:${IMAGE_TAG}" "${IMAGE_REPO}:latest"
  echo "Tagged: ${IMAGE_REPO}:latest"
fi

# Keep the newly built tag and latest alias; remove older local tags for same repo.
KEEP_TAG_1="${IMAGE_REPO}:${IMAGE_TAG}"
KEEP_TAG_2="${IMAGE_REPO}:latest"
OLD_LOCAL_TAGS="$(docker images "${IMAGE_REPO}" --format '{{.Repository}}:{{.Tag}}' \
  | awk -v keep1="${KEEP_TAG_1}" -v keep2="${KEEP_TAG_2}" '$1 != keep1 && $1 != keep2 { print $1 }' \
  | sort -u)"

if [[ -n "${OLD_LOCAL_TAGS}" ]]; then
  echo "Removing old local image tags:"
  echo "${OLD_LOCAL_TAGS}"
  while IFS= read -r old_tag; do
    [[ -z "${old_tag}" ]] && continue
    docker rmi "${old_tag}" >/dev/null 2>&1 || true
  done <<< "${OLD_LOCAL_TAGS}"
else
  echo "No old local tags to remove for ${IMAGE_REPO}."
fi

if [[ "${CLEAN_DANGLING}" == "true" ]]; then
  echo "Pruning dangling images..."
  docker image prune -f >/dev/null
fi

echo "Current local images for ${IMAGE_REPO}:"
docker images "${IMAGE_REPO}" --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}'

if [[ "${LOAD_TO_MINIKUBE}" == "true" ]]; then
  if ! command -v minikube >/dev/null 2>&1; then
    echo "ERROR: minikube is not installed or not in PATH"
    exit 1
  fi

  if ! minikube -p "${MINIKUBE_PROFILE}" status >/dev/null 2>&1; then
    echo "ERROR: minikube profile '${MINIKUBE_PROFILE}' is not running"
    exit 1
  fi

  if [[ "${CLEAN_MINIKUBE_OLD}" == "true" ]]; then
    OLD_MINIKUBE_TAGS="$(minikube -p "${MINIKUBE_PROFILE}" image ls 2>/dev/null \
      | grep "docker.io/${IMAGE_REPO}:" \
      | sed 's#^docker.io/##' \
      | awk -v keep1="${KEEP_TAG_1}" -v keep2="${KEEP_TAG_2}" '$1 != keep1 && $1 != keep2 { print $1 }' \
      | sort -u || true)"

    if [[ -n "${OLD_MINIKUBE_TAGS}" ]]; then
      echo "Removing old minikube image tags:"
      echo "${OLD_MINIKUBE_TAGS}"
      while IFS= read -r old_mk_tag; do
        [[ -z "${old_mk_tag}" ]] && continue
        minikube -p "${MINIKUBE_PROFILE}" image rm "${old_mk_tag}" >/dev/null 2>&1 || true
      done <<< "${OLD_MINIKUBE_TAGS}"
    else
      echo "No old minikube tags to remove for ${IMAGE_REPO}."
    fi
  fi

  echo "Loading image into minikube: ${IMAGE_REPO}:${IMAGE_TAG}"
  minikube -p "${MINIKUBE_PROFILE}" image load "${IMAGE_REPO}:${IMAGE_TAG}"

  if [[ "${KEEP_LATEST_ALIAS}" == "true" ]]; then
    echo "Loading image into minikube: ${IMAGE_REPO}:latest"
    minikube -p "${MINIKUBE_PROFILE}" image load "${IMAGE_REPO}:latest"
  fi

  echo "Current minikube images for ${IMAGE_REPO}:"
  minikube -p "${MINIKUBE_PROFILE}" image ls | grep "docker.io/${IMAGE_REPO}:" || true
fi

echo "Done. Use image: ${IMAGE_REPO}:${IMAGE_TAG}"