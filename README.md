# cryptobot — bulutta paper (simülasyon) ticareti

Bu depo, `cryptobot` paketini **ücretsiz GitHub Actions** üzerinde zamanlanmış olarak
çalıştırmak için hazırlanmıştır. Amaç: bilgisayarınız kapalıyken de bot çalışsın ve
telefonunuza bildirim göndersin.

## Depo düzeni

```
<repo kökü>/
├─ .gitignore                     # kök için opt-in: her şeyi yok say, sadece botu al
├─ .github/workflows/paper-trade.yml   # CI: 30 dakikada bir sınırlı paper koşusu
├─ README.md                      # bu dosya
├─ DEPLOY.md                      # kurulum, cache, uyarılar, ücret/minutes notları
└─ cryptobot/                     # Python 3.13 paketi + testleri + config
```

GitHub iş akışlarını **yalnızca depo kökündeki** `.github/workflows/` altından okur;
bu yüzden iş akışı `cryptobot/` içinde değil, kökte durur. Kök klasör `cryptobot/`
paketinin **bir üst dizinidir** — komutlar bu yüzden `cryptobot/requirements.txt` ve
`python -m cryptobot ...` şeklindedir.

## Önemli: yalnızca paper / simülasyon

Bu bot **sadece paper trading** yapar: gerçek emir gönderen hiçbir kod yolu yoktur.
Koşu içinde bu duruş `python -m cryptobot safety` ve
`cryptobot/tests/no_live_order_scan.py` ile ayrıca doğrulanır. Borsa API anahtarı
gerekmez; iş akışı yalnızca Binance'in herkese açık, imzasız mum (kline) uç noktasını
kullanır.

## Secret / anahtar yok

Depoda hiçbir gizli anahtar tutulmaz. Bildirim ayarları (ntfy konu adı vb.) GitHub
tarafında **repository Variable veya Secret** olarak tanımlanır ve depoya yazılmaz.
Ayrıntı: [DEPLOY.md](DEPLOY.md).

## Bulutta elle çalıştırma

1. Depoyu GitHub'a gönderin.
2. Depoda **Actions** sekmesini açın.
3. Sol listeden **paper-trade** iş akışını seçin.
4. **Run workflow** düğmesine basın (timeframe / süre / gün gibi girdileri
   değiştirmeden bırakabilirsiniz).
5. Koşu bitince raporlar ve ledger artifact olarak indirilebilir.

Telefon bildirimleri için gereken tek şey `CRYPTOBOT_NTFY_TOPIC` değişkenidir; o
tanımlı değilse koşu yine çalışır, bildirimler yalnızca yerel loglara yazılır.

Ayrıntılı bilgi, cache davranışı ve dürüst uyarılar için: [DEPLOY.md](DEPLOY.md).
Botun kendi dokümantasyonu: [cryptobot/README.md](cryptobot/README.md).
