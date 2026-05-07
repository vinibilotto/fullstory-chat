@echo off
echo Starting FullStory Chat at http://localhost:8000
cd /d "%~dp0"
..\python-embed\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
