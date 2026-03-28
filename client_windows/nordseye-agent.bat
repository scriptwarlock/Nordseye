@echo off
:: Nordseye Agent — Windows auto-restart watchdog
:: Place this .bat file next to agent.py and run it at startup.
:: Edit SERVER, NAME, and AGENT_PATH below to match your setup.

set AGENT_PATH=C:\nordseye\agent.py
set SERVER=192.168.1.100
set NAME=PC 01

:loop
echo [%TIME%] Starting Nordseye agent...
python "%AGENT_PATH%" --server %SERVER% --name "%NAME%"
echo [%TIME%] Agent stopped (exit code %ERRORLEVEL%). Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
