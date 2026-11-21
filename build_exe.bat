@echo off
REM ---------------------------------------------------------------
REM  Builds ChessAssistant.exe from chess_assistant_gui.py
REM  Requirements: Python 3.9+ installed (tick "Add Python to PATH")
REM  Usage: put this file next to chess_assistant_gui.py and double-click it.
REM ---------------------------------------------------------------

echo Installing build dependencies...
py -m pip install --upgrade pip
py -m pip install python-chess mss numpy pillow pyinstaller
if errorlevel 1 goto :fail

echo.
echo Building ChessAssistant.exe (this takes a minute or two)...
py -m PyInstaller --onefile --windowed --clean --name ChessAssistant chess_assistant_gui.py
if errorlevel 1 goto :fail

echo.
echo ===============================================================
echo  Done!  Your app is at:  dist\ChessAssistant.exe
echo.
echo  Tip: copy stockfish.exe into the same folder as ChessAssistant.exe
echo  and the app will find it automatically.
echo ===============================================================
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
