# DEPLOY â€” cryptobot'u Ã¼cretsiz GitHub Actions Ã¼zerinde Ã§alÄ±ÅŸtÄ±rma

Bu belge, botu **bilgisayarÄ±nÄ±z kapalÄ±yken** bulutta Ã§alÄ±ÅŸtÄ±rmak iÃ§in gereken her ÅŸeyi
anlatÄ±r: koÅŸu davranÄ±ÅŸÄ±, bildirim ayarlarÄ±, elle tetikleme, cache ile durum kalÄ±cÄ±lÄ±ÄŸÄ±,
dÃ¼rÃ¼st uyarÄ±lar ve public/private repo arasÄ±nda geÃ§iÅŸ.

Depo adresi: `https://github.com/ss4181/cryptobot`

---

## 1. Kurulum (tek seferlik)

1. Bu klasÃ¶rÃ¼ bir GitHub deposuna gÃ¶nderin (kÃ¶k klasÃ¶r `cryptobot/` paketinin bir
   Ã¼st dizinidir; iÅŸ akÄ±ÅŸÄ± kÃ¶kteki `.github/workflows/paper-trade.yml` dosyasÄ±dÄ±r).
2. Depo **Actions** sekmesini bir kez aÃ§Ä±p iÅŸ akÄ±ÅŸlarÄ±nÄ±n etkinleÅŸmesini onaylayÄ±n.
3. (Ä°steÄŸe baÄŸlÄ±) Bildirim ayarÄ±nÄ± yapÄ±n â€” bkz. bÃ¶lÃ¼m 3.

Python kurulumu, baÄŸÄ±mlÄ±lÄ±klar ve testler iÅŸ akÄ±ÅŸÄ±nÄ±n kendisi tarafÄ±ndan yapÄ±lÄ±r;
sunucu, kredi kartÄ± veya yerel kurulum gerekmez.

---

## 2. Bir koÅŸuda ne olur (per-run davranÄ±ÅŸ)

Ä°ÅŸ akÄ±ÅŸÄ± her tetiklendiÄŸinde, taze (cold) bir Ubuntu runner Ã¼zerinde sÄ±rayla:

1. **Checkout** + Python 3.13 kurulumu + `cryptobot/requirements.txt` baÄŸÄ±mlÄ±lÄ±klarÄ±.
2. **GÃ¼venlik duruÅŸu:** `python -m cryptobot safety` ve
   `python cryptobot/tests/no_live_order_scan.py` â€” gerÃ§ek emir gÃ¶nderen kod yolu
   olmadÄ±ÄŸÄ± kanÄ±tlanÄ±r.
3. **Ã‡evrimdÄ±ÅŸÄ± birim testleri** (aÄŸ yok, bildirim env'i yok).
4. **Veri indirme:** Binance'in imzasÄ±z/herkese aÃ§Ä±k kline uÃ§ noktasÄ±ndan mumlar
   (`download --timeframe --days`). Taze runner'da cache boÅŸ olduÄŸu iÃ§in bu adÄ±m
   zorunludur.
5. **Durum geri yÃ¼kleme (cache):** bir Ã¶nceki koÅŸudan kalan ledger ve bildirim
   gÃ¼nlÃ¼ÄŸÃ¼ Actions cache'inden geri alÄ±nÄ±r (en iyi Ã§aba; ilk koÅŸuda boÅŸ).
6. **Paper koÅŸusu (sÄ±nÄ±rlÄ± pencere):** gerÃ§ek zamanlÄ± modda
   `--duration-seconds` boyunca dÃ¶ner, sonra kendi kendine durur
   (`--offline` / `--replay` yok).
7. **UzlaÅŸtÄ±rma ve Ã§Ä±ktÄ±lar:** `verify`, `export` (CSV/JSON), gÃ¼nlÃ¼k `report`.
8. **Bildirimler:** ntfy (ve tanÄ±mlÄ±ysa Telegram/webhook) Ã¼zerinden telefon bildirimi;
   ayrÄ±ca `cryptobot/logs/notifications.jsonl` denetim kaydÄ±.
9. **Durum kaydetme (cache):** ledger + bildirim gÃ¼nlÃ¼ÄŸÃ¼ cache'e yazÄ±lÄ±r.
10. **Artifact'ler:** `cryptobot/reports/` + `cryptobot/logs/` ve `ledger.sqlite`
    14 gÃ¼n boyunca indirilebilir.
11. **KEEPALIVE:** `deploy/heartbeat.txt` bayat ise (iÃ§indeki tarih 20 gÃ¼nden eski)
    bugÃ¼nÃ¼n UTC tarihiyle gÃ¼ncellenip commit'lenip push'lanÄ±r (bkz. bÃ¶lÃ¼m 6).

Her koÅŸu **sÄ±nÄ±rlÄ± bir anlÄ±k gÃ¶rÃ¼ntÃ¼dÃ¼r** â€” 7/24 kesintisiz bir sÃ¼reÃ§ deÄŸildir.

---

## 3. Bildirim ayarlarÄ±

GitHub'da: **Settings â†’ Secrets and variables â†’ Actions**

| Ad | TÃ¼r | Zorunlu mu | AÃ§Ä±klama |
|---|---|---|---|
| `CRYPTOBOT_NTFY_TOPIC` | **Variable** (Secret da olur) | Bildirim iÃ§in evet | Uzun, rastgele ntfy konu adÄ±; telefonunuzdaki ntfy uygulamasÄ±na aynÄ± adÄ± girin. |
| `CRYPTOBOT_NTFY_HOST` | **Variable** | HayÄ±r | VarsayÄ±lan `ntfy.sh`. Kendi sunucunuz veya bir ayna (mirror) kullanmak isterseniz buraya yazÄ±n. |

- **Variable** Ã¶nerilir: ntfy konu adÄ± public `ntfy.sh` Ã¼zerinde bir kimlik bilgisi
  deÄŸil, bir kanal adÄ±dÄ±r; Variable olarak dÃ¶ndÃ¼rmek (rotate) daha kolaydÄ±r.
- Secret olarak da tanÄ±mlayabilirsiniz; bu durumda loglarda maskelenir.
- Ä°ÅŸ akÄ±ÅŸÄ± `vars.X || secrets.X` sÄ±rasÄ±nÄ± dener, yani ikisinden biri yeterlidir.
- HiÃ§biri tanÄ±mlÄ± deÄŸilse koÅŸu **yine baÅŸarÄ±lÄ± olur**: bildirimler yalnÄ±zca konsola ve
  `cryptobot/logs/notifications.jsonl` dosyasÄ±na yazÄ±lÄ±r.

Ä°steÄŸe baÄŸlÄ± diÄŸer Secret'lar (tanÄ±mlÄ±ysa kullanÄ±lÄ±r): `CRYPTOBOT_NTFY_TOKEN`,
`CRYPTOBOT_TELEGRAM_BOT_TOKEN`, `CRYPTOBOT_TELEGRAM_CHAT_ID`, `CRYPTOBOT_WEBHOOK_URL`.

---

## 4. Elle (manuel) koÅŸu

1. `https://github.com/ss4181/cryptobot` â†’ **Actions** sekmesi.
2. Sol listede **paper-trade** iÅŸ akÄ±ÅŸÄ±nÄ± seÃ§in.
3. SaÄŸda **Run workflow** â†’ dal (branch) seÃ§in â†’ **Run workflow**.
4. Ä°steÄŸe baÄŸlÄ± girdiler: `timeframe` (1m..1d), `duration_seconds` (sÄ±nÄ±rlÄ± pencere
   saniyesi), `interval_seconds` (dÃ¶ngÃ¼ler arasÄ± saniye), `days` (indirilecek geÃ§miÅŸ
   gÃ¼n sayÄ±sÄ±). BoÅŸ bÄ±rakÄ±lÄ±rsa 1h / 300 / 60 / 30 kullanÄ±lÄ±r.
5. KoÅŸu bitince Ã¶zet ekranÄ±ndan artifact'leri indirebilirsiniz.

---

## 5. Cache ile durum kalÄ±cÄ±lÄ±ÄŸÄ±

Taze bir runner'da disk her seferinde boÅŸtur; bu yÃ¼zden ledger `actions/cache` ile
koÅŸular arasÄ±nda taÅŸÄ±nÄ±r:

- **Yol (path):** `cryptobot/data/ledger.sqlite` ve
  `cryptobot/logs/notifications.jsonl`
- **Anahtar (key):** `cryptobot-state-${{ github.run_number }}` (her koÅŸuda yeni)
- **Geri yÃ¼kleme anahtarÄ± (restore-keys):** `cryptobot-state-` â†’ yani mevcut koÅŸuya
  ait anahtar yoksa **en yeni** `cryptobot-state-*` girdisi kullanÄ±lÄ±r.
- **SÄ±ra:** geri yÃ¼kleme, paper koÅŸusundan **Ã¶nce**; kaydetme, koÅŸu/export'lardan
  **sonra** ve artifact yÃ¼klemesinden **Ã¶nce**.
- Her iki adÄ±m da **en iyi Ã§aba**dÄ±r (`continue-on-error`): ilk koÅŸuda dosya yoktur,
  cache boÅŸalabilir (7 gÃ¼n eriÅŸilmezse silinir veya repo cache kotasÄ± aÅŸÄ±lÄ±rsa tahliye
  edilir). Bu durumda yalnÄ±zca **geÃ§miÅŸ** kaybedilir; koÅŸu asla baÅŸarÄ±sÄ±z olmaz.
- Artifact'ler cache'ten baÄŸÄ±msÄ±z, elle inceleyebileceÄŸiniz kopyadÄ±r.

**Durumu sÄ±fÄ±rlamak** (Ã¶rneÄŸin bozuk bir ledger'dan sonra temiz baÅŸlangÄ±Ã§):

1. **Actions â†’ Caches** (veya Settings â†’ Actions â†’ Caches) bÃ¶lÃ¼mÃ¼nden `cryptobot-state-*`
   girdilerini silin. Silinen cache geri gelmez; bir sonraki koÅŸu sÄ±fÄ±rdan baÅŸlar.
2. Ä°sterseniz depodaki `cryptobot/data/ledger.sqlite` dosyasÄ±nÄ± da silip commit'leyin.
   (Bu dosya `cryptobot/.gitignore` ile yok sayÄ±lÄ±r; bilinÃ§li olarak takip etmek
   istiyorsanÄ±z `git add -f` gerekir.)
3. Cache girdileri 7 gÃ¼n boyunca hiÃ§ eriÅŸilmezse GitHub tarafÄ±ndan kendiliÄŸinden
   silinir; bu yÃ¼zden uzun bir aradan sonra ilk koÅŸu geÃ§miÅŸi boÅŸ bulabilir.

---

## 6. DÃ¼rÃ¼st uyarÄ±lar (caveats)

- **Cron gecikmesi:** `schedule` girdisi `*/30 * * * *` olsa da GitHub zamanlanmÄ±ÅŸ
  koÅŸularÄ± geciktirebilir; yÃ¼ksek yÃ¼k zamanlarÄ±nda 30 dakika bir **alt sÄ±nÄ±r**tÄ±r,
  garanti deÄŸil. GitHub'Ä±n pratik minimumu ~5 dakikadÄ±r.
- **SÄ±nÄ±rlÄ± pencere, 7/24 deÄŸil:** her koÅŸu `duration_seconds` kadar Ã§alÄ±ÅŸÄ±r ve durur.
  Bu bir **anlÄ±k gÃ¶rÃ¼ntÃ¼dÃ¼r**; bot 24 saat ayakta deÄŸildir, dolayÄ±sÄ±yla mum kapanÄ±ÅŸlarÄ±
  arasÄ±nda kaÃ§an sinyal olabilir.
- **60 gÃ¼n hareketsizlik duraklamasÄ±:** GitHub, 60 gÃ¼n boyunca hiÃ§ depo aktivitesi
  olmayan repolarda `schedule` tetikleyicisini duraklatÄ±r. Bu depo bunu **KEEPALIVE**
  adÄ±mÄ±yla hafifletir: iÃ§indeki tarih 20 gÃ¼nden eskiyse `deploy/heartbeat.txt` bugÃ¼nÃ¼n
  UTC tarihiyle gÃ¼ncellenir ve push'lanÄ±r (yalnÄ±zca bu dosya commit'lenir).
  Push reddedilirse (fork, branch protection, salt-okunur token) iÅŸ **baÅŸarÄ±sÄ±z olmaz**;
  yalnÄ±zca duraklatma korumasÄ± devre dÄ±ÅŸÄ± kalÄ±r.
- **Cache kalÄ±cÄ± depolama deÄŸildir:** bkz. bÃ¶lÃ¼m 5; eviction durumunda geÃ§miÅŸ kaybolur.
- **Artifact sÃ¼resi 14 gÃ¼n:** indirmezseniz raporlar ve ledger 14 gÃ¼n sonra silinir.
- **AÄŸ eriÅŸimi:** veri kaynaÄŸÄ± Binance'in herkese aÃ§Ä±k uÃ§ noktasÄ±dÄ±r; GitHub runner'Ä±ndan
  eriÅŸilemezse koÅŸu veri adÄ±mÄ±nda baÅŸarÄ±sÄ±z olabilir. `CRYPTOBOT_NTFY_HOST` ile aynÄ±
  mantÄ±kla aÄŸ eriÅŸimi engellenen ortamlarda bildirimler iÃ§in ayna/self-host kullanÄ±n.
- **Paper only:** gerÃ§ek para/gerÃ§ek emir yok. Bu bir simÃ¼lasyondur; sonuÃ§lar yatÄ±rÄ±m
  tavsiyesi deÄŸildir.

---

## 7. Public â†” Private geÃ§iÅŸi ve Ã¼cretsiz dakikalar

| Repo tÃ¼rÃ¼ | Actions dakikalarÄ± | Not |
|---|---|---|
| **Public** | **SÄ±nÄ±rsÄ±z** (standart GitHub-hosted runner'larda) | Bu iÅŸ akÄ±ÅŸÄ± iÃ§in Ã¶nerilen mod. |
| **Private** | AylÄ±k **2.000** Ã¼cretsiz dakika (Free plan) | 30 dakikada bir, koÅŸu baÅŸÄ±na ~5-10 dk: aylÄ±k ~1.440 koÅŸu ile kota **kolayca aÅŸÄ±lÄ±r**. |

GeÃ§iÅŸ yapmak iÃ§in:

1. `https://github.com/ss4181/cryptobot` â†’ **Settings** â†’ sayfanÄ±n en altÄ± â†’ **Danger Zone**.
2. **Change repository visibility** â†’ **Make public** (veya **Make private**) â†’ onay
   iÃ§in depo adÄ±nÄ± yazÄ±p onaylayÄ±n.

Notlar:

- Public'e almak Ã¼cretsiz dakika sÄ±nÄ±rÄ±nÄ± kaldÄ±rÄ±r; kod ve commit geÃ§miÅŸi herkese aÃ§Ä±k
  olur. **Bu depoda secret tutulmaz** (bildirim konu adÄ± bir Variable'dÄ±r), ama yine de
  gizlilik beklentinizi gÃ¶zden geÃ§irin.
- Private'a alÄ±rsanÄ±z kotayÄ± aÅŸmamak iÃ§in `schedule` sÄ±klÄ±ÄŸÄ±nÄ± dÃ¼ÅŸÃ¼rÃ¼n (Ã¶rn.
  `0 * * * *` = saatte bir) veya `duration_seconds` deÄŸerini kÃ¼Ã§Ã¼ltÃ¼n (`workflow_dispatch`
  varsayÄ±lanÄ± 300 saniyedir; schedule koÅŸularÄ± da aynÄ± girdileri kullanÄ±r).
- Kota aÅŸÄ±mÄ± durumunda GitHub, kotayÄ± harcayan iÅŸ akÄ±ÅŸlarÄ±nÄ± durdurur; koÅŸu **baÅŸarÄ±sÄ±z
  olarak deÄŸil**, Ã§alÄ±ÅŸtÄ±rÄ±lmadan engellenmiÅŸ olarak gÃ¶rÃ¼nÃ¼r.
- Free plan private repo kotasÄ± 2.000 dakika/ay'dÄ±r; kurumsal/Ã¼cretli planlarda bu sayÄ±
  deÄŸiÅŸir. GÃ¼ncel deÄŸerleri GitHub'Ä±n "Billing" sayfasÄ±ndan doÄŸrulayÄ±n.
