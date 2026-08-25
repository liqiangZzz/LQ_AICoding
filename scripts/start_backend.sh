#!/bin/bash
cd /Users/Python/project/project-learn/pythod-code/LQ_AICoding
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
.venv/bin/python -m uvicorn agent.app:app --host 127.0.0.1 --port 2024