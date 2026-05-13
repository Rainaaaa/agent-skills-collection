#!/bin/bash
#SBATCH -J skills_probe_unlabeled
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --time=24:00:00
#SBATCH --mem=16G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection/log/%x_%j.err

# Probe SkillsMP for label coverage on the 103K downloaded-but-unlabeled
# skill_ids (skills that didn't match any L1 listing-page URL or sibling
# repo). Uses the existing crawl_details.py with a SYNTHETIC level1_dedup
# input (built by the derive_labels script), writing results to a
# SEPARATE output_dir so it never disturbs the main L1+L2 outputs.
#
# Three outcomes per URL:
#   - 200 + parseable detail page → entry appended to output_probe/level2_details.jsonl
#   - 200 but malformed           → logged in level2_request_log.jsonl
#   - 404 / network failure       → recorded in checkpoint as done; no detail entry
#
# Resumable: re-submit to pick up where it left off
# (checkpoint at output_probe/checkpoints/level2_checkpoint.json).
#
# Reset (rare):
#   sbatch --export=MODE=reset run_probe_unlabeled.sh

set -uo pipefail
PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection"
ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export PATH="${ENV_PREFIX}/bin:${PATH}"

PYTHON_BIN="${ENV_PREFIX}/bin/python"
CRAWLER_SCRIPT="${PROJECT_ROOT}/crawl_details.py"
JOBS_CONFIG="${PROJECT_ROOT}/crawler_jobs.json"
RUNTIME_CONFIG="${PROJECT_ROOT}/runtime_config.json"
INPUT_DIR="${PROJECT_ROOT}/input"
OUTPUT_DIR="${PROJECT_ROOT}/output_probe"   # ← isolated from main L2 outputs

MODE="${MODE:-resume}"
WORKERS="${WORKERS:-8}"

RESET_FLAG=""
[ "${MODE}" = "reset" ] && RESET_FLAG="--reset_checkpoint"

CMD=(
  "${PYTHON_BIN}" -u "${CRAWLER_SCRIPT}"
  --jobs_config    "${JOBS_CONFIG}"
  --runtime_config "${RUNTIME_CONFIG}"
  --input_dir      "${INPUT_DIR}"
  --output_dir     "${OUTPUT_DIR}"
  --workers        "${WORKERS}"
)
[ -n "${RESET_FLAG}" ] && CMD+=("${RESET_FLAG}")

echo "[INFO] MODE=${MODE} WORKERS=${WORKERS}"
echo "[INFO] OUTPUT_DIR=${OUTPUT_DIR}"
echo "[INFO] $(date)"
echo "[INFO] running: ${CMD[*]}"

cd "${PROJECT_ROOT}"
"${CMD[@]}"
RC=$?

echo
echo "[INFO] $(date) — probe finished. exit=${RC}"
if [ -f "${OUTPUT_DIR}/level2_details.jsonl" ]; then
  hits=$(wc -l < "${OUTPUT_DIR}/level2_details.jsonl")
  echo "[INFO] detail-page hits captured: ${hits}"
fi
