@echo off
setlocal EnableDelayedExpansion
title RINGSS Mission Control Launcher
cd /d "%~dp0"

echo =======================================================
echo   RINGSS OBSERVATORY SUITE - INICIALIZANDO
echo =======================================================
echo.

:: 1. Verificar el entorno sin cerrar procesos Python ajenos
echo [1/3] Verificando el entorno sin cerrar otros procesos Python...

:: 2. Identificar el interprete del entorno virtual
set "VENV_PYTHON="
if exist "%~dp0venv\Scripts\python.exe" (
    set "VENV_PYTHON=%~dp0venv\Scripts\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0env\Scripts\python.exe" (
    set "VENV_PYTHON=%~dp0env\Scripts\python.exe"
)

if not defined VENV_PYTHON (
    echo [ERROR CRITICO] No se encontro la carpeta del entorno virtual 'venv'.
    echo Asegurate de que la carpeta 'venv' este ubicada junto a este archivo .bat.
    echo.
    pause
    exit /b 1
)

echo [2/3] Entorno virtual detectado: "!VENV_PYTHON!"

:: 3. Activar el entorno virtual mediante su script oficial
if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
) else if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
) else if exist "%~dp0env\Scripts\activate.bat" (
    call "%~dp0env\Scripts\activate.bat"
)

echo [3/3] Iniciando launch_suite.py...
echo =======================================================
echo.

:: 4. Ejecutar la suite utilizando la ruta explicita del venv
"!VENV_PYTHON!" launch_suite.py %*

echo.
echo =======================================================
echo   La suite se ha cerrado (Codigo de salida: !errorlevel!)
echo =======================================================
pause