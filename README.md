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

Odoo každých 5 minút samo obnoví iba prevádzkové metriky, pre ktoré je jeho
databáza autoritatívnym zdrojom: otvorené P0/P1 incidenty, vek posledného
úspešného šifrovaného off-host backupu, vek posledného úspešného restore testu
s checksumom, aplikačným smoke a tenant-isolation testom a vek poslednej
úspešnej synchronizácie. Incident count môže byť pravdivá nula. Chýbajúci
backup, restore alebo sync dôkaz sa neprezentuje ako nula: príslušný current
ani history riadok sa nevytvorí. Cron nikdy nečíta Arcigy databázu, storage,
credentials ani raw telemetriu a nevytvára zdrojové synchronizácie.

Z rovnakých Odoo evidence záznamov sa odvádza aj výsledok posledného restore
testu, explicitne namerané RPO/RTO a posledný reprezentatívny capacity test.
RPO/RTO bez `rpo_measured`/`rto_measured` sa vynechá. Capacity readiness, vek
load testu a najvyššia bezpečne overená concurrency sa vytvorí iba pre záznam
s `representative=true`, uzavretým časom a konkrétnou `architecture_version`.
Restore a capacity event sa do histórie zapíše raz cez hash evidence kľúča;
5-minútový cron ho nekopíruje ako nový event.

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

### Backup, restore, load a data-quality dôkazy

`integrations/saas_operational_sync.mjs` posiela do Odoo iba striktne povolené skalárne polia pre `saas.backup.run`, `saas.restore.test`, `saas.dr.drill`, `saas.load.test`, `saas.data.quality.run` a `saas.sync.run`. Odmieta raw logy, neznáme polia, URL s credentials, nezabezpečený transport a external keys bez prefixu `develop:` alebo `main:`.

Úspešný restore musí niesť checksum, aplikačný smoke, tenant-isolation a
explicitné RPO/RTO measurement markery. Reprezentatívny load test musí mať
ukončenie, pozitívnu concurrency a verziu architektúry; p99 nesmie byť nižšie
ako p95 a error rate musí zostať v rozsahu 0–100 %.

Event-pipeline KPI sa odvodzujú iba z uzavretého data-quality záznamu s
`event_stream_complete=true` a úplným scalar kontraktom. Odhad straty je
`max(events_sent - events_received - retry_adjustment_count, 0)`. Počty,
deduplikácia, schema failures, chýbajúce polia, late events a unknown tenant
mapping musia byť navzájom konzistentné; duplicate a late rate sa pri nulovom
počte prijatých eventov vynechajú namiesto falošnej nuly.

Úplné event okno navyše povinne deklaruje maximálny absolútny clock skew,
processing-lag p95 a dead-letter count. Samostatný
`metric_quality_contract_complete=true` kontrakt pokrýva freshness,
completeness, uniqueness, validity, consistency, reconciliation, outliers,
unexpected zero/volume spike, numerator-over-denominator, neplatné záporné
hodnoty a missing dimensions. Každý výsledný počet je ohraničený explicitnou
eligible populáciou. Pri `eligible_metric_count=0` sa percentá vynechajú;
pravdivé nulové violation counts zostanú dostupné.

Úplný externý Odoo sync pokus používa rovnaký no-write-first adapter s modelom
`saas.sync.run`, ale zapisuje cez samostatnú idempotentnú metódu. Vyžaduje
uzavreté timestamps, úplné a navzájom konzistentné read/create/update/skip/reject
počty, rozpad API chýb, retry, backlog a `oldest_unsynced_at`. `error_code` môže
byť iba krátky symbolický kód, nie raw provider odpoveď. Existujúce interné Odoo
ingest behy zostávajú kompatibilné, ale bez `sync_contract_complete=true` sa z
nich nesmú odvodiť kompletné sync/backlog KPI.

Backup, restore a disaster-recovery dôkazy používajú samostatné explicitné
complete-contract markery. Len úplné kontrakty môžu vytvoriť KPI pre trvanie,
veľkosť, PITR/WAL, sekundárnu kópiu, šifrovanie, restore checksum a smoke,
chýbajúce záznamy, failover/failback, DNS, runbook a otvorené remediation kroky.
Staré záznamy bez markerov zostávajú kompatibilné, ale tieto nové KPI z nich
nevzniknú. DR príklad je v `integrations/saas_dr_evidence.example.json`.

Najprv vždy spustite no-write validáciu:

```powershell
node integrations/saas_operational_sync.mjs `
  --config=integrations/saas_operational_sync.example.json `
  --evidence=integrations/saas_operational_evidence.example.json `
  --dry-run
```

Ostrý beh vyžaduje `ARCIGY_ODOO_API_KEY` zo schváleného secret store. Reálny artifact držte mimo repozitára alebo ako `integrations/*.local.json` (gitignored). Najprv sa overuje Develop, potom Main; príkladový evidence súbor nikdy nepoužívajte ako reálny dôkaz.

### Doménové hodinové a denné agregáty

`integrations/saas_aggregate_sync.mjs` validuje a odosiela iba agregované riadky pre všetkých desať doménových modelov: tenant, endpoint, database, cache, queue, dependency, cost, product, security a capacity. Každý riadok musí mať presne hodinové alebo denné UTC okno, environment-prefixed `external_key`, povolené skalárne polia a konzistentné počty.

```powershell
node integrations/saas_aggregate_sync.mjs `
  --config=integrations/saas_aggregate_sync.example.json `
  --evidence=integrations/saas_aggregate_evidence.example.json `
  --dry-run
```

Reálne artifacts držte mimo repozitára alebo ako `integrations/*.local.json`. Dry-run nečíta API key a nerobí sieťovú požiadavku. Ostrý zápis povoľte až po source reconciliation a iba cez integration bot API key.

### Produktové eventy a denný rollup

`integrations/saas_product_event_rollup.mjs` spracuje export raw produktových eventov lokálne mimo Odoo a vytvorí iba anonymný `saas.product.daily` aggregate artifact. Vyžaduje celý verzovaný event envelope, UTC časy, deduplikovateľné `event_id`, SHA-256 user/object identifikátory a zhodné `develop`/`main` prostredie. Billing a authorization eventy prijme iba z explicitne povoleného serverového zdroja. PII/secret properties, konfliktné duplikáty a neuzavreté denné okná odmietne.

```powershell
node integrations/saas_product_event_rollup.mjs `
  --config=integrations/saas_product_event_rollup.example.json `
  --events=integrations/saas_product_events.example.json `
  > integrations/saas_product_daily.local.json

node integrations/saas_aggregate_sync.mjs `
  --config=integrations/saas_aggregate_sync.example.json `
  --evidence=integrations/saas_product_daily.local.json `
  --dry-run
```

Raw export držte v schválenom analytics úložisku alebo mimo repozitára; do Odoo ani Gitu nepatrí. Rollup pravdivo počíta iba priamo dokázateľné denné signups, meaningful active users/tenants a successful core actions. Activation, retention, feature adoption a time-to-value zámerne nevymýšľa bez úplného cohort/eligibility vstupu.

Rovnaký verzovaný export môže `integrations/saas_security_event_rollup.mjs` previesť na anonymný `saas.security.daily` artifact. Nulové hodnoty smie emitovať iba pri explicitnom `security_stream_complete: true`; inak failne, aby neúplný export nevydával za zdravý stav. Billing, auth, permission, cross-tenant, security a webhook eventy musia pochádzať zo schváleného serverového zdroja.

```powershell
node integrations/saas_security_event_rollup.mjs `
  --config=integrations/saas_security_event_rollup.example.json `
  --events=integrations/saas_product_events.local.json `
  > integrations/saas_security_daily.local.json

node integrations/saas_aggregate_sync.mjs `
  --config=integrations/saas_aggregate_sync.example.json `
  --evidence=integrations/saas_security_daily.local.json `
  --dry-run
```

Security rollup posiela iba denné počty login pokusov/zlyhaní, rate-limitov, suspicious loginov, cross-tenant denial/exposure, privileged actions, webhook signature failures a audit delivery failures. User, tenant, session, request, trace ani raw audit údaje v jeho výstupe nie sú.

### Reconciliation zdrojov

`integrations/saas_reconciliation_rollup.mjs` porovnáva iba agregované scalar totals pre sedem povinných kontrol: payment provider/Odoo invoices, app subscription/billing provider, measured/invoiced usage, app tenant/Odoo partner, active/paid seats, cloud invoice/cost import a observability/business request totals. Z rozdielu a explicitnej tolerancie vytvorí idempotentný `saas.data.quality.run` artifact so stavom `valid`, `warning` alebo `invalid`.

```powershell
node integrations/saas_reconciliation_rollup.mjs `
  --input=integrations/saas_reconciliation.example.json `
  > integrations/saas_reconciliation.local.json

node integrations/saas_operational_sync.mjs `
  --config=integrations/saas_operational_sync.example.json `
  --evidence=integrations/saas_reconciliation.local.json `
  --dry-run
```

Adapter neprijíma raw zákaznícke záznamy, credentials ani neznáme typy reconciliation. Rozdiel je `comparison_value - authoritative_value`; authoritative source a tolerancia musia byť schválené ownerom danej metriky pred ostrým použitím.

### Business KPI bridge

`integrations/saas_business_sync.mjs` je striktne allowlistovaný JSON-2 bridge pre 226 existujúcich KPI z produktového funnelu, engagementu, tenant health, revenue/billing, marketing/CRM, supportu, FinOps, privacy, release outcomes, AI-change risk, AI/LLM produktu a engineering quality. Prijíma iba agregované scalar hodnoty za uzavretú UTC hodinu, deň alebo kalendárny mesiac. Každý historický kľúč musí obsahovať správny `develop`/`main` a source prefix; dimenzované hodnoty musia mať samostatný non-global scope.

```powershell
node integrations/saas_business_sync.mjs `
  --config=integrations/saas_business_sync.example.json `
  --evidence=integrations/saas_business_evidence.example.json `
  --dry-run
```

Bridge odmieta raw customer payloady, neznáme KPI, vysokokardinalitné user/object identifikátory, cross-environment keys, neuzavreté obdobia, insecure URLs a credentials. Príklad je iba syntetický kontrakt a nesmie sa ingestovať ako reálny business dôkaz. Ostrý zápis povoľte až po schválení authoritative source, definície metriky, tolerancií a source reconciliation.

### Privacy a compliance agregácie

`integrations/saas_privacy_rollup.mjs` prijíma iba globálne agregáty z explicitne pomenovaných zdrojov: data inventory, DSR workflow, retention jobs, consent registry, privacy audit a governance. Každý zdroj musí označiť export ako úplný; inak kompilátor odmietne aj nulové hodnoty. Raw DSR, osoby, tenanty, emaily a auditné záznamy schéma nepozná.

```powershell
node integrations/saas_privacy_rollup.mjs `
  --input=integrations/saas_privacy_evidence.example.json `
  > integrations/saas_privacy_daily.local.json

node integrations/saas_business_sync.mjs `
  --config=integrations/saas_business_sync.example.json `
  --evidence=integrations/saas_privacy_daily.local.json `
  --dry-run
```

Denné a mesačné kontrakty sa nesmú miešať. Percentá vyžadujú numerátor aj denominátor a p95 čas DSR vyžaduje sample size. Príklad je iba syntetický; reálny export musí pochádzať zo schválených privacy systémov a pred Odoo zápisom prejsť reconciliation.

### GitHub CI/CD a security

`integrations/saas_github_sync.mjs` číta iba agregovateľné metadata z GitHub Actions, Dependabot a Secret Scanning. Do Odoo neposiela kód, mená vývojárov, raw alerty ani literalne secrets (`hide_secret=true`). Develop a Main sú viazané na samostatné branche a repository-wide security stav sa smie priradiť iba jednému prostrediu.

```powershell
$env:ARCIGY_GITHUB_READ_TOKEN = '<read-only secret-store reference>'
node integrations/saas_github_sync.mjs `
  --config=integrations/saas_github_sync.example.json `
  --dry-run
```

Fine-grained GitHub token potrebuje iba repository permissions `Actions: read`, `Dependabot alerts: read` a `Secret scanning alerts: read`; nepotrebuje Contents write ani administračné mutácie. Adapter pravdivo počíta build success, deployment frequency, lead time a otvorené critical/secret-scan nálezy. `change_failure_rate` ani `release_rollback_rate` neodhaduje z failed workflow; vyžadujú dôkaz incidentu alebo rollbacku. Ostrý zápis navyše vyžaduje `ARCIGY_ODOO_API_KEY`.

### AI-assisted change risk

`integrations/saas_ai_change_rollup.mjs` kompiluje iba uzavreté agregované dôkazy z change inventory, review gates, release outcomes a schváleného risk policy. Pokrýva všetkých 30 metrík zo špecifikácie pre AI-assisted zmeny: veľkosť a rozsah zmien, citlivé oblasti, migrácie a dependencies, testy, human/security review, incidenty, rollbacky, hotfixy, escaped defects, regresie, reopen/repair a počty LOW/MEDIUM/HIGH/CRITICAL REVIEW REQUIRED.

```powershell
node integrations/saas_ai_change_rollup.mjs `
  --input=integrations/saas_ai_change_daily.example.json `
  > integrations/saas_ai_change_daily.local.json

node integrations/saas_business_sync.mjs `
  --config=integrations/saas_business_sync.example.json `
  --evidence=integrations/saas_ai_change_daily.local.json `
  --dry-run
```

Denné a mesačné kontrakty sú oddelené. Každý source musí byť explicitne kompletný a obsahovať celý svoj kontrakt. Pomer bez oprávnenej vzorky sa označí `available=false` s `no_eligible_sample`; adaptér ho vynechá namiesto vymyslenej nuly. Odoo nedostane kód, diff, názvy alebo cesty súborov, autorov, prompty, secrets ani produkčné dáta. Príklady sú syntetické a nesmú byť použité ako reálny dôkaz.

### AI/LLM product evidence

`integrations/saas_ai_llm_rollup.mjs` owns all 37 product AI signals from the optional
dashboard specification. The hourly contract covers requests, latency, provider behavior,
tool calls and safety. The daily contract covers tokens, cost, quality and complete tenant,
feature and model allocations. Cost-per metrics are verified against exact populations and
all three dimensional breakdowns must reconcile to the global totals.

```powershell
node integrations/saas_ai_llm_rollup.mjs `
  --input=integrations/saas_ai_llm_hourly.example.json `
  > integrations/saas_ai_llm_hourly.local.json

node integrations/saas_ai_llm_rollup.mjs `
  --input=integrations/saas_ai_llm_daily.example.json `
  > integrations/saas_ai_llm_daily.local.json
```

Every source must explicitly declare completeness before a zero is trusted. Missing sample
populations remain unavailable. Raw prompts, responses, identities and arbitrary payloads
are outside the schema. `model_code`, tenant and feature dimensions use non-global scopes;
the Odoo cockpit now resolves exact dimensional filters without silently forcing global rows.

### Release outcome a DORA evidence

`integrations/saas_release_outcome_rollup.mjs` spája schválený deployment registry s incidentmi a rollbackmi až po uzavretí UTC dňa. Na rozdiel od workflow heuristiky vyžaduje jeden konzistentný deployment population pre success rate, change-failure rate a rollback rate; rollback success navyše kontroluje voči explicitnému počtu rollback pokusov.

```powershell
node integrations/saas_release_outcome_rollup.mjs `
  --input=integrations/saas_release_outcome.example.json `
  > integrations/saas_release_outcome.local.json

node integrations/saas_business_sync.mjs `
  --config=integrations/saas_business_sync.example.json `
  --evidence=integrations/saas_release_outcome.local.json `
  --dry-run
```

Kontrakt pokrýva 16 metrík: deployment count/success/duration/queue, confirmed change failures, incidenty, rollback count/rate/attempts/success, hotfixy, restore time, canary failures, artifact mismatch a environment drift. Raw workflow logy, commit SHA, mená aktérov a deployment IDs neprijíma. Príklad je syntetický a nie je produkčný dôkaz.

### Engineering quality evidence

`integrations/saas_engineering_quality_rollup.mjs` compiles exactly six complete daily
aggregate contracts: pull requests, branches, CI, tests, feature flags and architecture.
It emits 57 allowlisted metrics and keeps Develop and Main in independent keys. Counts
must be explicit zeroes; sample-based metrics without an eligible population must be
marked unavailable instead of being presented as healthy zeroes.

```powershell
node integrations/saas_engineering_quality_rollup.mjs `
  --input=integrations/saas_engineering_quality.example.json `
  > integrations/saas_engineering_quality.local.json

node integrations/saas_business_sync.mjs `
  --config=integrations/saas_business_sync.example.json `
  --evidence=integrations/saas_engineering_quality.local.json `
  --dry-run
```

The compiler rejects partial sources, unknown fields, raw logs, code or file paths,
identities, credentials in URLs, inconsistent ratios and impossible cross-metric
populations. The included input is synthetic and is not production evidence.

## CapRover nasadenie

Canonical repo teraz obsahuje aj zachovaný `geotherm_drive`, CapRover `captain-definition`, produkčné Python dependencies a riadené `ODOO_INIT_MODULES`/`ODOO_UPDATE_MODULES`. Presný backup, smoke a rollback postup je v `docs/SAAS_CONTROL_CENTER_DEPLOY_RUNBOOK.md`.

Aktuálny read-only audit ukazuje 44 % využitie root filesystemu. Pred veľkým buildom stav znovu zmerajte a vždy zachovajte aktívny aj predchádzajúci funkčný image každej služby.
