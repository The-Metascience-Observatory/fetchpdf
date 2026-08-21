"""Shared pytest configuration.

The suite is offline and CPU-bound, so it parallelizes cleanly; `-n auto` is
set in pyproject.toml and the hook below decides what "auto" means.
"""

import os


def pytest_xdist_auto_num_workers(config):
    """min(8, cores-1) workers: leave one core for the rest of the machine,
    and stop at 8 -- past that, worker startup outweighs a 400-test suite.

    (An explicit -n or PYTEST_XDIST_AUTO_NUM_WORKERS still wins over this.)
    """
    return max(1, min(8, (os.cpu_count() or 2) - 1))
