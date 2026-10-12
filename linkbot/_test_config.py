# linkbot/_test_config.py — 테스트용: config.py 가 없으면(새 PC·CI) config.example.py 를 config 로 등록
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(HERE, "config.py")) and "config" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("config", os.path.join(HERE, "config.example.py"))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    sys.modules["config"] = _mod
