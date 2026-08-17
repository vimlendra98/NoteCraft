@echo off
REM ---------------------------------------------------------------------------
REM  NoteCraft build  ->  dist\NoteCraft\NoteCraft.exe
REM  No spec file: everything is on the PyInstaller command line.
REM ---------------------------------------------------------------------------
setlocal

echo [1/4] Installing dependencies...
python -m pip install --upgrade --quiet -r requirements-build.txt
if errorlevel 1 goto :failed

echo [2/4] Drawing the application icon...
python notecraft.py --export-icon NoteCraft.ico
if errorlevel 1 goto :failed

echo [3/4] Freezing...
python -m PyInstaller --noconfirm --clean --windowed --name NoteCraft --icon NoteCraft.ico ^
 --exclude-module PyQt6.QtNetwork --exclude-module PyQt6.QtPrintSupport ^
 --exclude-module PyQt6.QtOpenGL --exclude-module PyQt6.QtOpenGLWidgets ^
 --exclude-module PyQt6.QtQml --exclude-module PyQt6.QtQuick ^
 --exclude-module PyQt6.QtQuick3D --exclude-module PyQt6.QtQuickWidgets ^
 --exclude-module PyQt6.QtWebEngineCore --exclude-module PyQt6.QtWebEngineWidgets ^
 --exclude-module PyQt6.QtWebChannel --exclude-module PyQt6.QtWebSockets ^
 --exclude-module PyQt6.QtMultimedia --exclude-module PyQt6.QtMultimediaWidgets ^
 --exclude-module PyQt6.QtSql --exclude-module PyQt6.QtTest ^
 --exclude-module PyQt6.QtDesigner --exclude-module PyQt6.QtHelp ^
 --exclude-module PyQt6.QtCharts --exclude-module PyQt6.QtDataVisualization ^
 --exclude-module PyQt6.QtBluetooth --exclude-module PyQt6.QtNfc ^
 --exclude-module PyQt6.QtPositioning --exclude-module PyQt6.QtSensors ^
 --exclude-module PyQt6.QtSerialPort --exclude-module PyQt6.QtRemoteObjects ^
 --exclude-module PyQt6.QtTextToSpeech --exclude-module PyQt6.QtPdf ^
 --exclude-module PyQt6.QtPdfWidgets --exclude-module PyQt6.QtXml ^
 --exclude-module PyQt6.QtSvgWidgets ^
 --exclude-module tkinter --exclude-module numpy --exclude-module matplotlib ^
 --exclude-module PIL --exclude-module pandas --exclude-module scipy ^
 --exclude-module unittest --exclude-module doctest --exclude-module pydoc_data ^
 --exclude-module lib2to3 --exclude-module idlelib --exclude-module setuptools ^
 --exclude-module pip --exclude-module smtplib --exclude-module ftplib ^
 notecraft.py
if errorlevel 1 goto :failed

echo [4/4] Removing Qt libraries NoteCraft never loads...
REM --exclude-module only drops Python modules; Qt's DLLs arrive as binary
REM dependencies and ignore it, so they are deleted here instead (~40 MB).
set QTBIN=dist\NoteCraft\_internal\PyQt6\Qt6\bin
if exist "%QTBIN%\opengl32sw.dll"      del /q "%QTBIN%\opengl32sw.dll"
if exist "%QTBIN%\d3dcompiler_47.dll"  del /q "%QTBIN%\d3dcompiler_47.dll"
if exist "%QTBIN%\Qt6Pdf.dll"          del /q "%QTBIN%\Qt6Pdf.dll"
if exist "%QTBIN%\Qt6Network.dll"      del /q "%QTBIN%\Qt6Network.dll"
del /q "%QTBIN%\Qt6Qml*.dll"           2>nul
del /q "%QTBIN%\Qt6Quick*.dll"         2>nul
if exist "dist\NoteCraft\_internal\PyQt6\Qt6\translations" ^
 rmdir /s /q "dist\NoteCraft\_internal\PyQt6\Qt6\translations"

echo.
echo  Done:  dist\NoteCraft\NoteCraft.exe
echo  Launch it once to confirm it starts, then zip dist\NoteCraft to share it.
echo  If it fails to start, re-run without step 4 (opengl32sw.dll is the usual cause).
goto :eof

:failed
echo.
echo  Build failed - see the messages above.
exit /b 1
