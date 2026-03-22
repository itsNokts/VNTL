@echo off
echo Building VNTL...
python -m ensurepip --upgrade
python -m pip install pyinstaller pyinstaller-hooks-contrib --quiet
python -m pip install -r requirements.txt --quiet
python -m PyInstaller vntl.spec --clean -y
echo.
echo Done! Distributable is in dist\vntl\
pause
