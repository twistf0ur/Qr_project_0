@echo off
REM Build a standalone Windows .exe for the Barcode Scanner app.
REM Uses BarcodeScanner.spec which bundles the beep WAV and the zbar DLLs.
REM Output: dist\BarcodeScanner.exe

python -m PyInstaller --noconfirm --clean BarcodeScanner.spec

echo.
echo Build finished. Check the dist folder.
pause