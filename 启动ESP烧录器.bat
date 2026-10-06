@echo off
rem ===================================================================
rem  ESP Flasher launcher.
rem  This file is ASCII-only on purpose so that cmd.exe parses it
rem  correctly under any console code page. The GUI itself is Chinese.
rem ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

python -c "import serial, rich_click, esp_pylib, esptool" >nul 2>nul
if errorlevel 1 goto INSTALL

goto RUN

:INSTALL
echo [INFO] Installing runtime dependencies, please wait...
python -m pip install -r requirements-gui.txt
if errorlevel 1 goto DEPSFAIL

:RUN
echo [INFO] Starting ESP Flasher GUI ...
python esp_flasher_gui.py %*
if errorlevel 1 goto RUNFAIL
goto END

:NOPYTHON
echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
echo         Download: https://www.python.org/downloads/
pause
goto END

:DEPSFAIL
echo [ERROR] Failed to install dependencies.
echo         Run manually: python -m pip install -r requirements-gui.txt
pause
goto END

:RUNFAIL
echo.
echo [ERROR] The program exited abnormally. See the messages above.
pause
goto END

:END
endlocal
