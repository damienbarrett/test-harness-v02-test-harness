export PATH := env_var('HOME') + "/.local/bin:" + env_var('HOME') + "/.cargo/bin:" + env_var('HOME') + "/go/bin:" + env_var('PATH')
LOCAL_HARNESS_DIR := justfile_directory() + "/.harness"
export HARNESS_DIR := env_var_or_default('HARNESS_DIR', LOCAL_HARNESS_DIR)
export HARNESS_OUTPUT_DIR := env_var_or_default('HARNESS_OUTPUT_DIR', HARNESS_DIR + "/outputs")
export HARNESS_CACHE_DIR := env_var_or_default('HARNESS_CACHE_DIR', HARNESS_DIR + "/cache")
export UV_CACHE_DIR := env_var_or_default('UV_CACHE_DIR', HARNESS_CACHE_DIR + "/test-harness/uv")

# List available commands
default:
    @just --list

# Install test harness dependencies
setup:
    uv run --with pyyaml python -c 'import yaml'
    uv run --with wasmtime==43.0.0 python -c 'import wasmtime'

# Run test harness self-checks
test:
    ./check-runner-parity.py

# Run test harness coverage checks
coverage: test

# Remove generated outputs while preserving dependency state
clean:
    rm -rf __pycache__ .pytest_cache .coverage htmlcov output "$HARNESS_OUTPUT_DIR/test-harness"

# Remove generated outputs and setup artifacts
purge: clean
    rm -rf .venv "$UV_CACHE_DIR" "{{LOCAL_HARNESS_DIR}}" "$HARNESS_CACHE_DIR/test-harness" "$HARNESS_OUTPUT_DIR/test-harness"
