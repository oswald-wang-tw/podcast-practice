"""Filesystem capabilities vary between Windows developer and CI machines."""

import pytest


@pytest.fixture
def make_symlink():
    def create(link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink tests require Developer Mode or symlink privileges")
            raise

    return create
