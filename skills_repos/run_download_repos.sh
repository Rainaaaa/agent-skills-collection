#!/bin/bash
#SBATCH -J skills_repos_download
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --time=24:00:00
#SBATCH --mem=16G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/skills_repos/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/skills_repos/log/%x_%j.err

# Stage 3 — download GitHub archives and build per-skill package views.
# Reads metadata produced by Stage 2 to pick the canonical default branch
# and apply license / status / star filters.
#
# Variables (export via sbatch --export=…):
#   REPO_MAP, SKILLS_DEDUP   override input paths
#   WORKERS                  parallel downloads (default from runtime_config.json)
#   FORCE                    1 = re-download even if .ready.json exists
#   MAX_REPOS                cap repos this run (smoke test)
#
#   REQUIRE_LICENSE          1 = drop repos with no SPDX license
#   LICENSE_WHITELIST        comma-separated SPDX ids
#   LICENSE_BLACKLIST        comma-separated SPDX ids
#   SKIP_ARCHIVED            1 = drop archived repos
#   SKIP_DISABLED            1 = drop disabled repos
#   SKIP_FORKS               1 = drop forks
#   MIN_STARS                int

set -uo pipefail

PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/skills_repos"
LABELS_OUTPUT="/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection/output"

ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export CONDA_DEFAULT_ENV="AgentSkillsOSS"
export PATH="${ENV_PREFIX}/bin:${PATH}"
PYTHON_BIN="${ENV_PREFIX}/bin/python -u"

REPO_MAP="${REPO_MAP:-${LABELS_OUTPUT}/repo_map.json}"
SKILLS_DEDUP="${SKILLS_DEDUP:-${LABELS_OUTPUT}/dedup_index.json}"
WORKERS="${WORKERS:-}"
FORCE="${FORCE:-0}"
MAX_REPOS="${MAX_REPOS:-}"

REQUIRE_LICENSE="${REQUIRE_LICENSE:-0}"
LICENSE_WHITELIST="${LICENSE_WHITELIST:-}"
LICENSE_BLACKLIST="${LICENSE_BLACKLIST:-}"
SKIP_ARCHIVED="${SKIP_ARCHIVED:-0}"
SKIP_DISABLED="${SKIP_DISABLED:-0}"
SKIP_FORKS="${SKIP_FORKS:-0}"
MIN_STARS="${MIN_STARS:-0}"

cd "${PROJECT_ROOT}"

EXTRA=""
[ -n "${WORKERS}" ]            && EXTRA="${EXTRA} --workers ${WORKERS}"
[ "${FORCE}" = "1" ]           && EXTRA="${EXTRA} --force"
[ -n "${MAX_REPOS}" ]          && EXTRA="${EXTRA} --max_repos ${MAX_REPOS}"
[ "${REQUIRE_LICENSE}" = "1" ] && EXTRA="${EXTRA} --require_license"
[ -n "${LICENSE_WHITELIST}" ]  && EXTRA="${EXTRA} --license_whitelist ${LICENSE_WHITELIST}"
[ -n "${LICENSE_BLACKLIST}" ]  && EXTRA="${EXTRA} --license_blacklist ${LICENSE_BLACKLIST}"
[ "${SKIP_ARCHIVED}" = "1" ]   && EXTRA="${EXTRA} --skip_archived"
[ "${SKIP_DISABLED}" = "1" ]   && EXTRA="${EXTRA} --skip_disabled"
[ "${SKIP_FORKS}" = "1" ]      && EXTRA="${EXTRA} --skip_forks"
[ "${MIN_STARS}" != "0" ]      && EXTRA="${EXTRA} --min_stars ${MIN_STARS}"

echo "[INFO] repo_map=${REPO_MAP}"
echo "[INFO] skills_dedup=${SKILLS_DEDUP}"
echo "[INFO] extra_args:${EXTRA}"

${PYTHON_BIN} download_repos.py \
  --runtime_config runtime_config.json \
  --repo_map       "${REPO_MAP}" \
  --skills_dedup   "${SKILLS_DEDUP}" \
  ${EXTRA}
