"""Checks that the FULL (licensed) CPLEX engine is reachable from this Python environment.

    python -m scripts.check_cplex

The community edition bundled with `pip install cplex` stops at 1,000 variables/constraints;
this script builds a 3,000-variable model so it only passes with your academic licence.
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    try:
        import cplex
        print(f"cplex       : {cplex.__version__}  ({cplex.__file__})")
    except ImportError:
        print("cplex       : NOT INSTALLED -> pip install cplex==<your Studio version>, then docplex config --upgrade")
        return 1
    try:
        import docplex
        from docplex.mp.model import Model
        print(f"docplex     : {docplex.__version__}")
    except ImportError:
        print("docplex     : NOT INSTALLED -> pip install docplex")
        return 1

    m = Model("size_check")
    x = m.integer_var_list(3000, ub=10, name="x")
    for k in range(0, 3000, 2):
        m.add_constraint(x[k] + x[k + 1] >= 1)
    m.minimize(m.sum((k % 7 + 1) * x[k] for k in range(3000)))
    try:
        sol = m.solve()
    except Exception as ex:
        sol, err = None, str(ex)
    else:
        err = str(m.solve_details.status)
    if sol is not None:
        print(f"FULL CPLEX OK: 3,000-variable model solved, objective {sol.objective_value:g}")
        return 0
    print("Size test FAILED - this is the COMMUNITY edition (limit 1,000 vars/constraints).")
    print(f"   solver message: {err[:200]}")
    print("   Fix: docplex config --upgrade \"<path to CPLEX_Studio folder>\"   (see SETUP.md)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
