"""Shared test fixtures."""

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_vharness_logger():
    """`main(["-q", ...])` reconfigures the shared logger; undo it for later tests."""
    logger = logging.getLogger("vharness")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers
