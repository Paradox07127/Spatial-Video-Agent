# ws pipeline environment: `source pipeline/env.sh` (no secrets here; service accounts stay in stack/config.env)
export SVA_ROOT=${SVA_ROOT:-/mnt/data/vast-hack}
export SVA_OUT=$SVA_ROOT/sva-out
export SVA_DATA=$SVA_ROOT/data
source ~/sva/.venv/bin/activate
