@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - fill in SEC_USER_AGENT and NTFY_TOPIC, then run this again.
  notepad .env
  exit /b
)
docker compose up -d --build
if errorlevel 1 (echo. & echo Is Docker Desktop running? & pause & exit /b)
start http://localhost:8080
