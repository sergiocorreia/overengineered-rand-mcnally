import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "code/common.do"
BOOTSTRAP = ROOT / "code/bootstrap_stata.do"
FAIL_CLOSED_TEST = ROOT / "code/test_common_fail_closed.do"


def executable_lines(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("*", "//")):
            continue
        lines.append(line)
    return "\n".join(lines).lower()


def test_ordinary_stata_commands_never_install_dependencies() -> None:
    ordinary = [path for path in (ROOT / "code").rglob("*.do") if path != BOOTSTRAP]
    for path in ordinary:
        source = executable_lines(path)
        assert "net install" not in source, path
        assert "ssc install" not in source, path
        assert not re.search(r"\brequire\b[^\n]*,\s*install\b", source), path


def test_common_validates_locally_and_bootstrap_is_explicit() -> None:
    common = executable_lines(COMMON)
    bootstrap = executable_lines(BOOTSTRAP)
    requirements = (ROOT / "code/requirements.txt").read_text(encoding="utf-8").lower()

    assert 'require using "$code/requirements.txt"' in common
    assert "setroot" not in common
    assert "setroot" not in requirements
    assert "ssc install require" in bootstrap
    assert 'require using "$code/requirements.txt", install' in bootstrap
    assert FAIL_CLOSED_TEST.is_file()
