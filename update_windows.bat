@echo off
setlocal
title Update Odysseus Docker Deployment

pushd "%~dp0" >nul

echo =========================================
echo Updating Odysseus Docker deployment
echo =========================================
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo [!] Git was not found on PATH.
  echo     Install Git for Windows, then run this script again.
  goto :fail
)

where docker >nul 2>nul
if errorlevel 1 (
  echo [!] Docker was not found on PATH.
  echo     Start Docker Desktop, then run this script again.
  goto :fail
)

docker compose version >nul 2>nul
if errorlevel 1 (
  echo [!] Docker Compose is not available.
  echo     Update Docker Desktop, then run this script again.
  goto :fail
)

echo [+] Pulling latest code...
git pull --ff-only
if errorlevel 1 goto :fail

echo.
echo [+] Validating effective Docker Compose configuration...
docker compose config --quiet
if errorlevel 1 goto :fail

echo.
echo [+] Rebuilding and restarting containers...
docker compose up -d --build --force-recreate
if errorlevel 1 goto :fail

echo.
echo [+] Verifying Odysseus started with the selected image contract...
docker compose ps --status running odysseus | findstr /I "odysseus" >nul
if errorlevel 1 (
  echo [!] Odysseus did not remain running after rebuild.
  docker compose logs --tail=80 odysseus
  goto :fail
)

echo.
echo [+] Removing dangling Docker images...
docker image prune -f
if errorlevel 1 goto :fail

echo.
echo =========================================
echo Update completed successfully.
echo =========================================
goto :done

:fail
echo.
echo Update failed. Check the message above and try again.

:done
popd >nul
pause
