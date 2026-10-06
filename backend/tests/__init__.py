# No test reaches Retell (docs/RETELL-PLAN.md): every `python -m tests.<name>` imports this
# package first, so the guard is in place before any test code runs. See retell_guard.py.
from tests import retell_guard as _retell_guard

_retell_guard.install()
