"""Keyboard regressions run against the actual inline player and library scripts."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_keyboard_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the dependency-free JavaScript keyboard tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("keyboard.test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
