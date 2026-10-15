@echo off
rem sortandpost file-manager helper for Windows (Docker Desktop).
rem 1. Set the two folders below to LOCAL_DIR and SYNC_DIR from your .env.
rem 2. Double-click to run it, or start it at login (see packaging\helper\README.md).
rem Needs Python 3 from python.org or the Microsoft Store ("py" launcher).

set "LOCAL_DIR=C:\path\to\sortandpost-local"
set "SYNC_DIR=C:\path\to\shared\sortandpost"

rem pyw = no console window. Use "py" instead to see its output while testing.
start "" pyw -3 "%~dp0..\..\tools\sortandpost_helper.py" --local-dir "%LOCAL_DIR%" --sync-dir "%SYNC_DIR%"
