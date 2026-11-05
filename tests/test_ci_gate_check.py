"""Deliberately failing test. Throwaway: exists only to confirm the CI gate blocks the image build."""


def test_ci_gate_deliberate_failure():
    assert False, "deliberate failure to verify that a failing test blocks the image build"
