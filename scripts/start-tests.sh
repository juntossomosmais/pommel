#!/usr/bin/env bash

# https://www.gnu.org/software/bash/manual/bash.html#The-Set-Builtin
# -e  Exit immediately if a command exits with a non-zero status.
set -e

coverage run -m unittest discover -s plugins/conditional-rules-plugin/hooks/conditional_rules -p 'test_*.py' -v
coverage report -m \
  --include='plugins/conditional-rules-plugin/hooks/conditional_rules/*' \
  --omit='*/test_*.py,*/_test_helpers.py'
