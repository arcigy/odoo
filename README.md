# Geotherm Odoo ERP - lokálne testovanie

Tento Docker stack používa oficiálny Odoo 19 Community image, PostgreSQL a addon `Geotherm Chatbot`.

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

