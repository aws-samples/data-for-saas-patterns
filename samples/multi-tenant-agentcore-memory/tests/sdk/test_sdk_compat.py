# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
test_sdk_compat.py — Runs sdk_compat_check.py against the installed SDKs.

The unit tests stub both SDKs, so this is the test that notices when a new
strands-agents or bedrock-agentcore release changes an internal the session
manager depends on. Skipped when the SDKs are not installed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"


def _sdk_installed() -> bool:
    probe = "import strands, bedrock_agentcore.memory.integrations.strands.session_manager"
    return subprocess.run([sys.executable, "-c", probe], capture_output=True).returncode == 0


def test_session_manager_against_real_sdk():
    if not _sdk_installed():
        pytest.skip("strands-agents / bedrock-agentcore not installed")
    env = dict(os.environ, PYTHONPATH=str(SRC), AWS_REGION="us-east-1")
    result = subprocess.run(
        [sys.executable, str(HERE / "sdk_compat_check.py")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
