@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python이 설치되어 있지 않거나 PATH에 없습니다.
  echo https://www.python.org/downloads/ 에서 Python 3를 설치하세요.
  echo 설치 첫 화면의 "Add python.exe to PATH"를 켜 주세요.
  echo.
  pause
  exit /b 1
)

python -c "import PIL, winocr" >nul 2>&1
if errorlevel 1 (
  echo 필요한 도구를 설치하고 있습니다. 잠시만 기다려 주세요.
  python -m pip install pillow winocr
  if errorlevel 1 (
    echo 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
    pause
    exit /b 1
  )
)

echo Snaplish 캡처 감지를 시작합니다.
echo 이 창을 닫으면 번역 알림이 멈춥니다.
echo 브라우저 주소: http://127.0.0.1:8765
echo.
python snaplish_server.py
echo.
echo 감지가 끝났습니다. 이 창을 닫아도 됩니다.
pause
