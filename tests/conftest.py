import json

import pytest

from app import config


@pytest.fixture
def business():
    return json.loads((config.ROOT / "sample_business.json").read_text())
