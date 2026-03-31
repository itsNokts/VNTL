@echo off
echo Building VNTL...
uv sync --group dev
uv run pyinstaller vntl.spec --clean -y
echo.
echo Done! Distributable is in dist\vntl\
pause
