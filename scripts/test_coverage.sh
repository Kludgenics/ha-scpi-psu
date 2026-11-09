#!/usr/bin/env bash
set -euo pipefail
pytest -q --cov=custom_components/scpi_psu --cov=packages/scpi_psu_client/scpi_psu_client --cov-report=
mv .coverage .coverage.test
coverage combine
coverage report --show-missing --fail-under=95
