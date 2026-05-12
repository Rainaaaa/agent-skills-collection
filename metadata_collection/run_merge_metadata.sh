#!/bin/bash
#SBATCH -J skills_labels_merge
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=2
#SBATCH --time=00:30:00
#SBATCH --mem=8G
#SBATCH -A r00954
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=cz1@iu.edu
#SBATCH --output=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection/log/%x_%j.log
#SBATCH --error=/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection/log/%x_%j.err

set -euo pipefail

# Merge L1 + L2 outputs into training-ready files and produce
# drop-in replacements for skills_download/config.json.
#
# Runs fast (minutes). Can be launched directly (no sbatch) if preferred:
#   python merge_metadata.py

PROJECT_ROOT="/N/slate/cz1/GitHub/AgentSkills-OSS/agent-skills-collection/metadata_collection"

# Bypass `conda activate` (Lustre-resilient).
ENV_PREFIX="/N/slate/cz1/conda/envs/AgentSkillsOSS"
export CONDA_PREFIX="${ENV_PREFIX}"
export CONDA_DEFAULT_ENV="AgentSkillsOSS"
export PATH="${ENV_PREFIX}/bin:${PATH}"

cd "${PROJECT_ROOT}"
"${ENV_PREFIX}/bin/python" -u merge_metadata.py --runtime_config "${PROJECT_ROOT}/runtime_config.json" \
                            --input_dir      "${PROJECT_ROOT}/input" \
                            --output_dir     "${PROJECT_ROOT}/output"
echo "[INFO] merge finished."
