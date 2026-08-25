@echo off
cd /d C:\Users\Python\project\project-learn\pythod-code\LQ_AICoding
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m uvicorn agent.app:app --host 127.0.0.1 --port 2024