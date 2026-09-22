@echo off
rem ==========================================================================
rem  cryptobot - baslatici (mobil bildirimler dahil)
rem  PowerShell betik kisitlamasindan ETKILENMEZ (.ps1 yerine .cmd)
rem
rem  Kullanim (PowerShell veya cmd icinde):
rem     .\start-bot.cmd                 -> botu paper modda baslatir
rem     .\start-bot.cmd notify status   -> bildirim kurulumu (aktif/pasif olaylar)
rem     .\start-bot.cmd notify test     -> telefona test bildirimi
rem     .\start-bot.cmd status          -> bot durumu
rem     .\start-bot.cmd stop            -> botu durdurur
rem     .\start-bot.cmd backtest --timeframe 1h
rem
rem  ONEMLI - GIZLILIK: ntfy.sh / ntfy.envs.net gibi UCRETSIZ genel
rem  sunucularda konu adini bilen HERKES mesajlarinizi okuyabilir; mesaj
rem  ucuncu taraf bir yansiya (mirror) ugrar. Gizlilik istiyorsaniz kendi
rem  ntfy sunucunuzu kurun (CRYPTOBOT_NTFY_HOST) veya Telegram kullanin.
rem
rem  ONEMLI - KAPSAM: Makine geneli CRYPTOBOT_NTFY_TOPIC TANIMLAMAYIN.
rem  Kalici (User/System) bir degisken, makinedeki HER Python surecine
rem  gecer; test/ dogrulama kosulari da gercek bildirim gondermeye
rem  baslar. Bu betik degiskenleri SADECE kendi baslattigi surece verir.
rem ==========================================================================
setlocal EnableExtensions

rem --------------------------------------------------------------------------
rem  AYAR: ntfy sunucusu. Degistirmek icin SADECE bu satiri duzenleyin.
rem  Kendi sunucunuz icin orn: set "NTFY_HOST_DEFAULT=ntfy.ornek.com"
rem --------------------------------------------------------------------------
set "NTFY_HOST_DEFAULT=ntfy.sh"

rem Konu adi SIR DEGILDIR (tahmin edilmesi zor bir kanal adidir). Kullanici
rem zaten bir deger tanimladiysa ona SAYGI duyulur; yoksa bu betige ozel
rem varsayilan kullanilir. Makine geneli degisken GEREKMEZ.
if not defined CRYPTOBOT_NTFY_TOPIC set "CRYPTOBOT_NTFY_TOPIC=cryptobot-paper-6c6de613c4-db65cc46fe"
if not defined CRYPTOBOT_NTFY_HOST set "CRYPTOBOT_NTFY_HOST=%NTFY_HOST_DEFAULT%"
rem Saatlik AG gonderim siniri. Varsayilan 20; gercek kullanimda islem
rem sayisi cok dusuk oldugu icin 60 rahat bir tavan. Yerel console/file
rem satirlari bu butceyi HIC tuketmez.
if not defined CRYPTOBOT_NOTIFY_MAX_PER_HOUR set "CRYPTOBOT_NOTIFY_MAX_PER_HOUR=60"

echo [bildirim] ntfy host : %CRYPTOBOT_NTFY_HOST%
echo [bildirim] ntfy konu : %CRYPTOBOT_NTFY_TOPIC%
echo [bildirim] kapsam    : yalnizca BU islem icin (makine geneli degisken gerekmez)
echo [bildirim] gizlilik  : konu adini bilen herkes mesaji okuyabilir; kendi ntfy
echo              sunucunuz veya Telegram bunu onler (bkz. NOTIFICATIONS.md)
echo.

set "HERE=%~dp0"
if not exist "%HERE%scripts\paperbot.py" (
  echo [hata] Bulunamadi: "%HERE%scripts\paperbot.py"
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  python3 "%HERE%scripts\paperbot.py" %*
) else (
  if "%~1"=="" (
    python "%HERE%scripts\paperbot.py" run
  ) else (
    python "%HERE%scripts\paperbot.py" %*
  )
)
exit /b %ERRORLEVEL%
