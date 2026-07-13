# Google Drive integration audit

## Current ERP scope

The ERP currently contains:

- `crm.lead` records enriched by the Geotherm chatbot;
- chatbot transcripts and lead payloads stored on the lead;
- pricebook products, questions, addons, areas and Excel import history;
- anonymous chatbot analytics;
- native Odoo `ir.attachment` records.

There are currently no native sale orders, quotations, invoices, HR records, projects or accounting models in this deployment. Drive integration therefore must not pretend that those workflows are already present.

## Drive source of truth

The local path `C:\Users\laube\Môj disk\FIRMA_STRUKTURA` is a Google Drive-synced local view. The server must never depend on that Windows path. Runtime integration uses the Google Drive API and resolves folders by Drive IDs and exact folder names.

The existing naming and structure are preserved:

| ERP concern | Drive location | Direction |
| --- | --- | --- |
| CRM lead/client project | `40_OPERATIONS_AND_CLIENTS/02_Client_Projects/<lead>` | Odoo to Drive |
| Discovery attachments | `<lead>/01_Discovery` | Odoo to Drive |
| Execution, delivery, archive | `<lead>/02_Execution`, `<lead>/03_Delivery`, `<lead>/04_Archive` | Reserved for future workflows |
| AI intake | `01_AI_INBOX` | Reserved for a future controlled import worker |
| Finance, HR, legal, branding, tech | Existing company map folders | Not connected until matching ERP models exist |

## Implemented behavior

The `geotherm_drive` custom addon provides:

1. OAuth 2.0 web-server authorization with offline access and a refresh token.
2. Root folder discovery by `FIRMA_STRUKTURA` name or configured folder ID.
3. A lead action that creates the client project folder and the four standard subfolders.
4. Idempotent upload/update of Odoo attachments linked to the lead.
5. Drive file mapping records with checksum, URL, parent ID and sync state.

The first release is deliberately one-way and non-destructive: it does not delete Drive files, move existing files, or import arbitrary Drive files into Odoo. Those operations need explicit classification and audit rules first.

## Required production configuration

Set these values in CapRover app `geotherm-odoo` or the Odoo settings screen:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_DRIVE_REDIRECT_URI`
- `GOOGLE_DRIVE_ROOT_FOLDER_NAME=FIRMA_STRUKTURA` or `GOOGLE_DRIVE_ROOT_FOLDER_ID`
- `GOOGLE_OAUTH_STATE_SECRET`

The refresh token is stored in the CapRover environment or in Odoo's protected configuration parameter after OAuth consent. Never commit it to GitHub.

## Next integration phases

1. Controlled `01_AI_INBOX` import with classification and approval.
2. Native quotation/project models and their Drive folders.
3. Finance invoice routing after accounting data exists.
4. Explicit inbound Drive-to-Odoo import with duplicate detection and audit log.
