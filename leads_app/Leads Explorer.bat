@echo off
cd /d "%~dp0"
rem If leads.duckdb is missing, the app asks whether to create a new empty database.
start "" pythonw app.py
