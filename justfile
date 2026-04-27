export PATH := env_var('HOME') + "/.local/bin:" + env_var('HOME') + "/.cargo/bin:" + env_var('HOME') + "/go/bin:" + env_var('PATH')
export UV_CACHE_DIR := env_var_or_default('UV_CACHE_DIR', '.cache/uv')

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
    rm -rf __pycache__ .pytest_cache .coverage htmlcov output

# Remove generated outputs and setup artifacts
purge: clean
    rm -rf .venv
