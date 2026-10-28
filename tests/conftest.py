"""
Общие настройки pytest: asyncio-режим.
"""
import pytest

# Все тесты с async def запускаются через pytest-asyncio
pytest_plugins = ["pytest_asyncio"]


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
