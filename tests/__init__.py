"""Standard-library unittest suite for the ``kiro_adapters`` package.

This package marker exists so that ``python -m unittest discover -s tests -t .``
can import the test modules as ``tests.test_*``. Individual modules also add
this directory to ``sys.path`` themselves, so ``from _support import ...``
works under both ``-t .`` and bare ``-s tests`` discovery.

Nothing in here touches the real ``~/.kiro`` tree or a real ``kirocrew`` /
``kirocode`` install: every binary is a fake shell script in a temporary
directory.
"""
