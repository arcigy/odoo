# Geotherm Odoo ERP - lokálne testovanie

Tento Docker stack používa oficiálny Odoo 19 Community image, PostgreSQL a addon `Geotherm Chatbot`.

Stack obsahuje aj samostatný addon `Arcigy SaaS Control Center`. Existujúce Geotherm CRM, chatbot, cenník, analytika a náklady nemení.

## Spustenie

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose logs -f odoo
```

Odoo bude na `http://127.0.0.1:8069`. Pri prvej lokálnej databáze je používateľ `admin` a heslo `admin`; po prvom prihlásení ho zmeňte. Heslá a API kľúče v `.env.example` sú iba lokálne vzory.

## Čo addon obsahuje

- `Geotherm Chatbot > Leady`: natívne Odoo CRM leady, celý výcuc projektu a transcript.
- `Geotherm Chatbot > Cenník`: produkty, otázky, doplnky a oblasti služieb.
- `Geotherm Chatbot > Cenník > Nahrať Excel`: import existujúcej Geotherm šablóny aj pôvodného Vaillant Excelu.
- `Geotherm Chatbot > Dashboard`: konverzácie, správy a leady podľa oblasti.
- nový lead vytvorí obchodníkovi Odoo aktivitu `Zavolať`.
- `Arcigy SaaS > Develop | Main`: bezpečne oddelené SaaS KPI, incidenty, backup/restore, load testy a Odoo sync stav.
- `Geotherm Google Drive`: zachovaný jednosmerný a nedeštruktívny lead/attachment sync do existujúcej firemnej Drive štruktúry.

Import `Doplniť a aktualizovať` páruje položky podľa oblasti, názvu a modelu. Import `Nahradiť celý cenník` deaktivuje staré položky a zapne položky z Excelu. Ručné zmazanie v Odoo je tiež možné.

## Prepojenie chatbota

Do koreňového `.env` nastavte rovnaký API kľúč a secret ako v `odoo/.env`:

```dotenv
ODOO_API_KEY=change-this-api-key
ODOO_WEBHOOK_SECRET=change-this-webhook-secret
ODOO_LEAD_URL=http://127.0.0.1:8069/geotherm/api/v1/leads
ODOO_ANALYTICS_URL=http://127.0.0.1:8069/geotherm/api/v1/events
ODOO_PRICEBOOK_URL=http://127.0.0.1:8069/geotherm/api/v1/pricebook
ODOO_PRICEBOOK_REFRESH_MS=60000
```

Po zmene cenníka v Odoo sa chatbot zosynchronizuje automaticky. Ručný refresh je dostupný na `POST /admin/pricebook/refresh` s koreňovým `ADMIN_TOKEN`.

## API

| Metóda | Endpoint | Účel |
| --- | --- | --- |
| `GET` | `/geotherm/api/v1/health` | stav Odoo integrácie |
| `POST` | `/geotherm/api/v1/leads` | vytvorenie alebo aktualizácia CRM leadu |
| `POST` | `/geotherm/api/v1/events` | jedna anonymizovaná analytická udalosť za turn |
| `GET` | `/geotherm/api/v1/pricebook` | aktuálny normalizovaný cenník pre chatbot |

API prijíma `Authorization: Bearer <ODOO_API_KEY>`. POST endpointy akceptujú aj existujúci podpis `X-Arcigy-Signature: sha256=...` nad reťazcom `<timestamp>\n<body>`.

## Produkčné poznámky

Lokálny stack je určený na API a workflow testovanie. Pred produkciou treba doplniť HTTPS reverse proxy, zálohovanie PostgreSQL aj filestore, reálne SMTP/Odoo Discuss notifikácie, silné tajomstvá a rozhodnúť, či klient používa Odoo Community alebo Enterprise.

## Arcigy SaaS Control Center

Každá aktuálna a historická metrika má povinné prostredie `develop` alebo `main`. Aktuálna hodnota je unikátna podľa `(metric, environment, scope)`, takže Develop nikdy neprepíše Main. Dashboard ich vždy zobrazuje v dvoch samostatných stĺpcoch.

Odoo nie je úložisko raw requestov, logov ani traces. Prijíma iba aktuálne alebo agregované hodnoty cez Odoo 19 JSON-2:

```text
POST /json/2/saas.metric.current/ingest_metric_batch
```

V Odoo vytvorte samostatného interného používateľa iba so skupinou `SaaS Integration Bot`, potom mu v `Preferences > Account Security` vytvorte časovo obmedzený API key. Kľúč držte iba v schválenom secret store; neukladajte ho do tohto repozitára, logov ani dashboardov.

Lokálny sync a presné premenné prostredia sú opísané v `kitchen_app/docs/SAAS_ODOO_CONTROL_CENTER.md`. Produkčný schedule, API key, deploy a zápisy do live Odoo vyžadujú samostatné schválenie a live smoke test.

### Prometheus zdroje pre DB, pool, queue, cache a dependencies

Samostatný adaptér `integrations/saas_prometheus_sync.mjs` číta iba agregované hodnoty. Pre každý query vyžaduje presne jeden scalar/vector sample a pri zdieľanom Prometheovi kontroluje explicitný environment marker. Develop a Main sa zbierajú a zapisujú ako dva nezávislé payloady.

Najprv upravte kópiu `integrations/saas_prometheus_sync.example.json` podľa skutočných exporterov a overte ju bez zápisu:

```powershell
$env:ARCIGY_PROM_DEVELOP_TOKEN = '<secret-store-reference>'
$env:ARCIGY_PROM_MAIN_TOKEN = '<secret-store-reference>'
node integrations/saas_prometheus_sync.mjs --config=integrations/saas_prometheus_sync.local.json --dry-run
```

Príkladové PromQL názvy s prefixom `arcigy_` sú kontrakt, nie tvrdenie, že exportery sú už nasadené. Ostrý beh povoľte až po kontrole dvoch dry-run výstupov, source reconciliation a uložení Odoo API key v schválenom secret store.

## CapRover nasadenie

Canonical repo teraz obsahuje aj zachovaný `geotherm_drive`, CapRover `captain-definition`, produkčné Python dependencies a riadené `ODOO_INIT_MODULES`/`ODOO_UPDATE_MODULES`. Presný backup, smoke a rollback postup je v `docs/SAAS_CONTROL_CENTER_DEPLOY_RUNBOOK.md`.

Aktuálny live server bol pri read-only audite na 97 % zaplnenia. Pred buildom alebo deployom treba samostatne schváliť bezpečnú image-retention úpravu, ktorá zachová aktívny aj predchádzajúci funkčný image každej služby.
