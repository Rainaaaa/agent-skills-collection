#!/bin/bash
#SBATCH -J skills_repos_fetch_metadata
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=2
#SBATCH --time=24:00:00
#SBATCH --mem=4G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/AgentSkills-collection/skills_repos/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/AgentSkills-collection/skills_repos/log/%x_%j.err

# Stage 2 — fetch GitHub repo metadata + license for the repos discovered by
# Stage 1 (metadata_collection/output/repo_map.json).
#
# Variables (export via sbatch --export=…):
#   REPO_MAP            override default repo_map.json path
#   WITH_LICENSE_TEXT   1 = also fetch /license body (~2x request volume)
#   MAX_REPOS           cap repos this run (smoke test)
#   FORCE               1 = refetch repos already in the checkpoint
#
# Examples:
#   sbatch run_fetch_metadata.sh
#   sbatch --export=WITH_LICENSE_TEXT=1 run_fetch_metadata.sh
#   sbatch --export=MAX_REPOS=200 run_fetch_metadata.sh    # smoke

set -uo pipefail

PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/AgentSkills-collection/skills_repos"
LABELS_OUTPUT="/N/slate/cz1/GitHub/AgentSkills-OSS/AgentSkills-collection/metadata_collection/output"

ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export CONDA_DEFAULT_ENV="AgentSkillsOSS"
export PATH="${ENV_PREFIX}/bin:${PATH}"
PYTHON_BIN="${ENV_PREFIX}/bin/python -u"

REPO_MAP="${REPO_MAP:-${LABELS_OUTPUT}/repo_map.json}"
WITH_LICENSE_TEXT="${WITH_LICENSE_TEXT:-0}"
MAX_REPOS="${MAX_REPOS:-}"
FORCE="${FORCE:-0}"

cd "${PROJECT_ROOT}"

EXTRA=""
[ "${WITH_LICENSE_TEXT}" = "1" ] && EXTRA="${EXTRA} --with_license_text"
[ "${FORCE}" = "1" ]             && EXTRA="${EXTRA} --force"
[ -n "${MAX_REPOS}" ]            && EXTRA="${EXTRA} --max_repos ${MAX_REPOS}"

echo "[INFO] repo_map=${REPO_MAP}"
echo "[INFO] extra_args:${EXTRA}"

${PYTHON_BIN} fetch_metadata.py \
  --runtime_config runtime_config.json \
  --tokens_file    tokens.json \
  --repo_map       "${REPO_MAP}" \
  ${EXTRA}
