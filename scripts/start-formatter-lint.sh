#!/usr/bin/env bash

# https://www.gnu.org/software/bash/manual/bash.html#The-Set-Builtin
# -e  Exit immediately if a command exits with a non-zero status.
set -e

ruff check --fix --exit-non-zero-on-fix
ruff format --exit-non-zero-on-fix
