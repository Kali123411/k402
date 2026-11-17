# `python -m k402.spendctl …` — the operator's CLI for a kUSD agent's spending (see k402/spend.py: main).
# A separate module so `-m` does not re-execute k402.spend, which the package already imports.
from .spend import main

if __name__ == "__main__":
    raise SystemExit(main())
