# =============================================================================
#  cryptobot - baslatici (mobil bildirimler dahil)
#
#  Kullanim:
#     .\start-bot.ps1                     -> paper modda botu baslatir
#     .\start-bot.ps1 stop                -> calisan botu durdurur
#     .\start-bot.ps1 status              -> durum
#     .\start-bot.ps1 notify status       -> bildirim kurulumu (aktif/pasif olaylar)
#     .\start-bot.ps1 notify test         -> telefona test bildirimi gonderir
#     .\start-bot.ps1 backtest --timeframe 1h
#
#  Konu adini degistirmek icin (adli parametre):
#     .\start-bot.ps1 -Topic "kendi-konu-adiniz" notify test
#
#  ONEMLI - GIZLILIK: ntfy.sh gibi UCRETSIZ genel sunucularda
#  konu adini bilen HERKES mesajlarinizi okuyabilir; mesaji ucuncu taraf bir
#  yansiya (mirror) da ugrar. Gizlilik icin kendi ntfy sunucunuzu kurun
#  (-NtfyHost) veya Telegram kullanin.
#
#  ONEMLI - KAPSAM: degiskenler SADECE bu betigin baslattigi surece verilir.
#  Makine geneli (User/System) CRYPTOBOT_NTFY_TOPIC TANIMLAMAYIN: kalici
#  degisken makinedeki her Python surecine gecer ve test/dogrulama kosulari da
#  gercek bildirim gondermeye baslar (bkz. NOTIFICATIONS.md).
#
#  NOT: Konu adi SIR DEGILDIR (herkese acik bir kanal adidir), yine de tahmin
#  edilmesi zor olmali. Telegram token gibi gercek sirlar bu dosyaya YAZILMAZ.
# =============================================================================
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Topic = "cryptobot-paper-6c6de613c4-db65cc46fe",
    # Kendi ntfy sunucunuz icin orn: -NtfyHost "ntfy.ornek.com"
    [string]$NtfyHost = "ntfy.sh",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BotArgs = @("run")
)

$ErrorActionPreference = "Stop"

# Yalnizca bu surec icin: makine geneli degisken gerekmez.
if ($Topic) { $env:CRYPTOBOT_NTFY_TOPIC = $Topic }
if ($NtfyHost -and -not $env:CRYPTOBOT_NTFY_HOST) { $env:CRYPTOBOT_NTFY_HOST = $NtfyHost }
# Saatlik AG gonderim siniri (yerel console/file bu butceyi tuketmez).
if (-not $env:CRYPTOBOT_NOTIFY_MAX_PER_HOUR) { $env:CRYPTOBOT_NOTIFY_MAX_PER_HOUR = "60" }

Write-Host "[bildirim] ntfy host : $env:CRYPTOBOT_NTFY_HOST"
Write-Host "[bildirim] ntfy konu : $env:CRYPTOBOT_NTFY_TOPIC"
Write-Host "[bildirim] kapsam    : yalnizca BU islem icin (makine geneli degisken gerekmez)"
Write-Host "[bildirim] gizlilik  : konu adini bilen herkes mesaji okuyabilir; kendi ntfy"
Write-Host "           sunucunuz veya Telegram bunu onler (bkz. NOTIFICATIONS.md)"

$launcher = Join-Path $PSScriptRoot "scripts\paperbot.py"
if (-not (Test-Path $launcher)) {
    Write-Error "Bulunamadi: $launcher"
    exit 1
}

python $launcher @BotArgs
exit $LASTEXITCODE
