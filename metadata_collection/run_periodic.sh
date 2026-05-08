#!/bin/bash
#SBATCH -J skills_labels_periodic
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --time=48:00:00
#SBATCH --mem=16G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection/log/%x_%j.err

# Periodic-run pipeline (run weekly / monthly to grow coverage):
#
#   Step 1. L1 with --reset_checkpoint: re-crawl every occupation/category leaf
#           page from scratch so newly-added skills surface.
#           Cards (level1_cards.jsonl) and dedup (level1_dedup.json) are
#           PRESERVED across runs — observations accumulate, dedup unions in
#           any new label memberships and adds rows for brand-new skills.
#
#   Step 2. L2 without reset: its done_job_keys checkpoint already lists every
#           skillmp_link we've extracted detail for, so this run only processes
#           the *delta* — new skills L1 just discovered.
#
# This single script runs both stages back-to-back. If the 10h SLURM cap is
# reached partway through L1, just resubmit; --reset_checkpoint only fires
# the FIRST time per "periodic run". (To resume an in-progress periodic run
# without re-resetting, sbatch with MODE=resume.)
#
# Examples:
#   sbatch run_periodic.sh                          # full periodic sweep (L1 reset → L2 delta)
#   sbatch --export=MODE=resume run_periodic.sh     # resume after cap hit, no L1 reset
#   sbatch --export=SKIP_L2=1 run_periodic.sh       # only refresh L1
#   sbatch --export=WORKERS=12 run_periodic.sh      # more concurrency (default 8)
#
#   # FAST incremental update — sort=recent only, stop scrolling each page once
#   # the tail of rendered cards is 50 already-known skills. Drastically faster
#   # than the full sweep once the dedup is large.
#   sbatch --export=INCREMENTAL=1 run_periodic.sh

set -uo pipefail

PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection"

# Bypass `conda activate` (which can hang for minutes when /N/slate Lustre is
# degraded) and just point PATH at the env's bin/ directly. Equivalent to
# what `conda activate AgentSkillsOSS` does for the runtime path.
ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export CONDA_DEFAULT_ENV="AgentSkillsOSS"
export PATH="${ENV_PREFIX}/bin:${PATH}"

PYTHON_BIN="${ENV_PREFIX}/bin/python -u"
JOBS_CONFIG="${PROJECT_ROOT}/crawler_jobs.json"
RUNTIME_CONFIG="${PROJECT_ROOT}/runtime_config.json"
INPUT_DIR="${PROJECT_ROOT}/input"
OUTPUT_DIR="${PROJECT_ROOT}/output"

MODE="${MODE:-fresh}"            # fresh = reset L1 checkpoint; resume = don't
WORKERS="${WORKERS:-8}"
SKIP_L2="${SKIP_L2:-0}"
INCREMENTAL="${INCREMENTAL:-0}"  # 1 = sort=recent + early-stop on known streak

L1_RESET=""
[ "${MODE}" = "fresh" ] && L1_RESET="--reset_checkpoint"

L1_INCREMENTAL_FLAGS=""
if [ "${INCREMENTAL}" = "1" ]; then
  L1_INCREMENTAL_FLAGS="--sort_modes recent --early_stop_known_streak 50"
fi

cd "${PROJECT_ROOT}"

echo
echo "======================================================================"
echo "[STEP 1/2] L1 list-page sweep (MODE=${MODE}, WORKERS=${WORKERS}, INCREMENTAL=${INCREMENTAL})"
echo "======================================================================"
${PYTHON_BIN} crawl_lists.py \
  --jobs_config    "${JOBS_CONFIG}" \
  --runtime_config "${RUNTIME_CONFIG}" \
  --input_dir      "${INPUT_DIR}" \
  --output_dir     "${OUTPUT_DIR}" \
  --source         all \
  --workers        "${WORKERS}" \
  ${L1_RESET} ${L1_INCREMENTAL_FLAGS}
L1_RC=$?
echo "[STEP 1/2] L1 exit=${L1_RC}"

if [ "${SKIP_L2}" = "1" ]; then
  echo "[STEP 2/2] SKIP_L2=1, exiting after L1"
  exit ${L1_RC}
fi

if [ ${L1_RC} -ne 0 ]; then
  echo "[STEP 2/2] L1 did not finish cleanly; skipping L2 (rerun this script with MODE=resume to continue L1)"
  exit ${L1_RC}
fi

echo
echo "======================================================================"
echo "[STEP 2/2] L2 detail sweep (delta only — uses existing done_job_keys)"
echo "======================================================================"
${PYTHON_BIN} crawl_details.py \
  --jobs_config    "${JOBS_CONFIG}" \
  --runtime_config "${RUNTIME_CONFIG}" \
  --input_dir      "${INPUT_DIR}" \
  --output_dir     "${OUTPUT_DIR}" \
  --workers        "${WORKERS}"
L2_RC=$?
echo "[STEP 2/2] L2 exit=${L2_RC}"
echo "[INFO] periodic run finished."
exit ${L2_RC}
