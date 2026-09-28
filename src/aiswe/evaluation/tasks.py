"""Fixed evaluation tasks: each sets up a small scratch repo and a task
description, and is scored by whether the test suite passes afterward. Start
small (3 tasks) and grow this list over time -- see PLAN.md Roadmap v2. Not
a SWE-bench subset yet; these are hand-authored to be cheap and fast to run
repeatedly while iterating on prompts/routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalTask:
    id: str
    files: dict[str, str]
    task: str
    timeout_s: int = 240


TASKS: list[EvalTask] = [
    EvalTask(
        id="add_function",
        files={
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
        task=(
            "Add a multiply(a, b) function to calc.py with a corresponding test "
            "in test_calc.py, then run the tests and commit."
        ),
    ),
    EvalTask(
        id="fix_failing_test",
        files={
            "calc.py": "def divide(a, b):\n    return a / b\n",
            "test_calc.py": (
                "import pytest\n\nfrom calc import divide\n\n\n"
                "def test_divide():\n    assert divide(10, 2) == 5\n\n\n"
                "def test_divide_by_zero():\n    with pytest.raises(ValueError):\n        divide(1, 0)\n"
            ),
        },
        task=(
            "test_divide_by_zero is currently failing. Fix divide() in calc.py so it "
            "raises ValueError on division by zero, then run the tests and commit."
        ),
    ),
    EvalTask(
        id="multi_file_symbol_search",
        files={
            "auth/service.py": (
                "class AuthService:\n    def login(self, username, password):\n"
                "        return username == 'admin' and password == 'secret'\n"
            ),
            "main.py": (
                "from auth.service import AuthService\n\n\n"
                "def handle_request(username, password):\n"
                "    return AuthService().login(username, password)\n"
            ),
            "test_auth.py": (
                "from auth.service import AuthService\n\n\n"
                "def test_login_success():\n"
                "    assert AuthService().login('admin', 'secret') is True\n\n\n"
                "def test_login_failure():\n"
                "    assert AuthService().login('admin', 'wrong') is False\n"
            ),
        },
        task=(
            "Find the login function (it's not in the top-level file) and add input "
            "validation: it should return False (not raise) if username or password "
            "is empty or None, instead of comparing directly. Add a test for this case "
            "in test_auth.py, run the tests, and commit."
        ),
    ),
]
