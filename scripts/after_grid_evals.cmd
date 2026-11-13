@echo off
rem Waits for the running grid pass to finish, then runs the evaluation-only jobs requested in the 19 Oct
rem review (GPU free by then). Run from the repo root: cmd /c .\scripts\after_grid_evals.cmd
:wait
findstr /C:"grid pass finished" cf_data\logs\grid_stdout.log >nul
if errorlevel 1 (
  timeout /t 300 /nobreak >nul
  goto wait
)
timeout /t 60 /nobreak >nul
set PYTHONUNBUFFERED=1
echo [%time%] after-grid evaluations start >> cf_data\logs\after_grid_evals.log
rem 1) untrained base student as a reference point (diversity, sampled quality)
.venv\Scripts\python.exe cf.py eval --run 135m --split test >> cf_data\logs\after_grid_evals.log 2>&1
rem 2) calibration: ECE, accuracy, NLL, Brier, reliability diagram
.venv\Scripts\python.exe analysis\calibration.py >> cf_data\logs\after_grid_evals.log 2>&1
rem 3) analyses with all seeds (CPU)
.venv\Scripts\python.exe analysis\bootstrap.py >> cf_data\logs\after_grid_evals.log 2>&1
.venv\Scripts\python.exe analysis\frontier.py >> cf_data\logs\after_grid_evals.log 2>&1
.venv\Scripts\python.exe analysis\length_controlled.py >> cf_data\logs\after_grid_evals.log 2>&1
echo [%time%] after-grid evaluations done >> cf_data\logs\after_grid_evals.log
