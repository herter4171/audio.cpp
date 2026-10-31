#!/usr/bin/env bash
# Build audio.cpp (CUDA) with the MiniMax Music 3 model.
#
# Portable across hosts with the same repo layout (e.g. this box and spx1).
# Idempotent: safe to re-run; incremental builds only recompile what changed.
#
# Env overrides:
#   CUDAToolkit_ROOT  force the CUDA toolkit dir (else /usr/local/cuda, else nvcc on PATH)
#   JOBS              parallel compile jobs (else nproc)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT/build"
JOBS="${JOBS:-$(nproc)}"

# --- CUDA toolkit discovery --------------------------------------------------
if [[ -n "${CUDAToolkit_ROOT:-}" && -d "${CUDAToolkit_ROOT}" ]]; then
  CUDA_ROOT="${CUDAToolkit_ROOT}"
elif [[ -d /usr/local/cuda ]]; then
  CUDA_ROOT=/usr/local/cuda
elif command -v nvcc >/dev/null 2>&1; then
  CUDA_ROOT="$(cd "$(dirname "$(command -v nvcc)")/.." && pwd)"
else
  echo "error: no CUDA toolkit found (try: export CUDAToolkit_ROOT=/path/to/cuda)" >&2
  exit 1
fi
NVCC="$CUDA_ROOT/bin/nvcc"
[[ -x "$NVCC" ]] || { echo "error: $NVCC not found" >&2; exit 1; }
echo "CUDA toolkit: $CUDA_ROOT"
"$NVCC" --version | tail -2

# --- Configure ----------------------------------------------------------------
# -DENGINE_ENABLE_CUDA=ON          CUDA backend
# -DCMAKE_CUDA_ARCHITECTURES=native  build only for the GPUs present on THIS host
# -DAUDIOCPP_MODEL_SET=custom       keep the build small: one model, not the full catalog
cmake -S "$ROOT" -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DENGINE_ENABLE_CUDA=ON \
  -DCUDAToolkit_ROOT="$CUDA_ROOT" \
  -DCMAKE_CUDA_COMPILER="$NVCC" \
  -DCMAKE_CUDA_ARCHITECTURES=native \
  -DAUDIOCPP_MODEL_SET=custom \
  -DAUDIOCPP_MODELS=minimax_music3

# --- Build --------------------------------------------------------------------
# audiocpp_server: keeps the model hot across requests (used by run.sh)
# audiocpp_cli:    one-shot CLI, handy for debugging
cmake --build "$BUILD_DIR" --target audiocpp_server --target audiocpp_cli -j "$JOBS"

echo
echo "Built:"
echo "  $BUILD_DIR/bin/audiocpp_server"
echo "  $BUILD_DIR/bin/audiocpp_cli"
echo
echo "Start the hot server + web UI with: ./run.sh"
