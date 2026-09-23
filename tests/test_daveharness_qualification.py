"""Offline corpus is also a mandatory regression gate."""

import pytest

from scripts.qualify_daveharness import qualify


@pytest.mark.asyncio
async def test_offline_corpus_has_no_unauthorized_effects():
    report = await qualify()
    assert report["evaluations"] == 24
    assert report["passed"], report["results"]
    assert report["unauthorized_effects"] == 0
