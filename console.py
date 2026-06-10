"""Terminal output helpers shared by the devenv setup/run scripts."""

RULE = "*" * 78


class SetupException(Exception):
    """Raised by setup/build steps for an expected, user-facing failure.

    Caught at the top level of the host-side scripts; only the args are
    printed (no traceback).
    """


def print_red(text: str) -> None:
    print(f"\033[31m{text}\033[0m")


def print_green(text: str) -> None:
    print(f"\033[32m{text}\033[0m")


def print_rule() -> None:
    print(RULE)


def yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        ans = input(prompt + suffix).strip().lower()
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
