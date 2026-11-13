@echo off
rem Waits until the running grid pass has finished, then starts one more pass (finished jobs are skipped,
rem so only jobs added to cachekd/protocol.py since the first pass run). Run from the repo root.
:wait
findstr /C:"grid pass finished" cf_data\logs\grid_stdout.log >nul
if errorlevel 1 (
  timeout /t 300 /nobreak >nul
  goto wait
)
timeout /t 60 /nobreak >nul
echo [%time%] first pass finished, starting the second grid pass >> cf_data\logs\grid_stdout.log
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe cf.py grid --with-ablation --sft-init >> cf_data\logs\grid_stdout.log 2>&1
