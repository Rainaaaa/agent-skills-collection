#!/bin/bash
#SBATCH -J skills_labels_l1
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --time=10:00:00
#SBATCH --mem=16G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection/log/%x_%j.err

set -euo pipefail

# Examples:
# 1) Resume the full crawl (occupations + categories)
#    sbatch run_crawl_lists.sh
#
# 2) Occupations only
#    sbatch --export=SOURCE=occupation run_crawl_lists.sh
#
# 3) Categories only
#    sbatch --export=SOURCE=category run_crawl_lists.sh
#
# 4) Smoke test — first 4 jobs only
#    sbatch --export=MAX_JOBS=4 run_crawl_lists.sh
#
# 5) Reset the job pointer (keeps cards.jsonl + dedup so observations accumulate)
#    sbatch --export=MODE=reset run_crawl_lists.sh

PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection"

# Bypass `conda activate` (Lustre-resilient): just point PATH at env bin.
ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export CONDA_DEFAULT_ENV="AgentSkillsOSS"
export PATH="${ENV_PREFIX}/bin:${PATH}"

PYTHON_BIN="${ENV_PREFIX}/bin/python"
CRAWLER_SCRIPT="${PROJECT_ROOT}/crawl_lists.py"
JOBS_CONFIG="${PROJECT_ROOT}/crawler_jobs.json"
RUNTIME_CONFIG="${PROJECT_ROOT}/runtime_config.json"
INPUT_DIR="${PROJECT_ROOT}/input"
OUTPUT_DIR="${PROJECT_ROOT}/output"

MODE="${MODE:-resume}"
SOURCE="${SOURCE:-all}"
WORKERS="${WORKERS:-4}"
MAX_JOBS="${MAX_JOBS:-}"

RESET_FLAG=""
case "${MODE}" in
  resume) RESET_FLAG="" ;;
  reset)  RESET_FLAG="--reset_checkpoint" ;;
  *)
    echo "[ERROR] Unsupported MODE: ${MODE} (use resume|reset)"
    exit 1
    ;;
esac

CMD=(
  "${PYTHON_BIN}" -u "${CRAWLER_SCRIPT}"
  --jobs_config    "${JOBS_CONFIG}"
  --runtime_config "${RUNTIME_CONFIG}"
  --input_dir      "${INPUT_DIR}"
  --output_dir     "${OUTPUT_DIR}"
  --source         "${SOURCE}"
  --workers        "${WORKERS}"
)

if [[ -n "${RESET_FLAG}" ]]; then
  CMD+=("${RESET_FLAG}")
fi

if [[ -n "${MAX_JOBS}" ]]; then
  CMD+=(--max_jobs "${MAX_JOBS}")
fi

echo "[INFO] MODE=${MODE} SOURCE=${SOURCE} WORKERS=${WORKERS} MAX_JOBS=${MAX_JOBS:-unset}"
echo "[INFO] Running:"
printf ' %q' "${CMD[@]}"
echo

cd "${PROJECT_ROOT}"
"${CMD[@]}"

echo "[INFO] Crawl finished."
