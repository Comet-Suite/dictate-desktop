import pytest
from PyQt6.QtWidgets import QApplication

@pytest.fixture(scope='session')
def qt():
    return QApplication.instance() or QApplication(['dictate-test-suite'])
