# waves_sync

A [Frappe](https://frappeframework.com/) v16 custom app that exports ERPNext **Sales Invoice** data to the **Waves** application in its required JSON format, with full audit logging and a desk-based file-upload sync workflow.

---

## Table of Contents

1. [Overview](#overview)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [DocTypes](#doctypes)
6. [Usage](#usage)
   - [File Upload + Sync (Desk)](#file-upload--sync-desk)
   - [API Export (REST)](#api-export-rest)
   - [Full Workflow (Desk + API Push)](#full-workflow-desk--api-push)
7. [API Reference](#api-reference)
8. [JSON Format](#json-format)
9. [Development](#development)
   - [Project Structure](#project-structure)
   - [Running Tests](#running-tests)
   - [Sample Data](#sample-data)
10. [Troubleshooting](#troubleshooting)

---

## Overview

Waves is an external distribution/sales application used by the business. ERPNext holds the authoritative record of Sales Invoices and outstanding balances. This app bridges the two:

| Direction | What happens |
|-----------|-------------|
| ERPNext → Waves | Export submitted Sales Invoices (with outstanding > 0) to Waves JSON format |
| File Upload → Desk | Upload an existing Waves JSON file, validate each invoice row, optionally push to Waves API |

Every export or sync operation creates a **Waves Sync Log** entry with full audit trail - timestamps, row counts, API response codes, and per-invoice status.

---

## Requirements

- Frappe Framework v16
- ERPNext v16
- Python 3.10+
- `requests` package (bundled with Frappe's virtualenv)

---

## Installation

```bash
# From the bench root
cd /path/to/frappe-bench

# Add the app to the bench
bench get-app https://github.com/npkwild/waves_sync
# OR for local development:
# pip install -e apps/waves_sync (already in virtualenv)

# Add to bench apps list (required before install-app)
echo "waves_sync" >> sites/apps.txt

# Install on your site
bench --site your-site.com install-app waves_sync

# Run migrations
bench --site your-site.com migrate
```

---

## Configuration

Navigate to **Waves Sync Settings** in the Frappe desk (search in the top bar).

### Export Configuration

| Field | Type | Description |
|-------|------|-------------|
| Default Company | Link → Company | Pre-fills the company filter when exporting |
| Default From Date | Date | Pre-fills the start date |
| Default To Date | Date | Pre-fills the end date |
| Include invoices with zero outstanding | Check | Off by default - only unpaid/partially paid invoices |

### Delivery Mode

| Option | Behaviour |
|--------|-----------|
| **Download JSON** | Export returns a downloadable `.json` file. No external call is made. |
| **Push to Waves API** | Export POSTs the JSON payload to the configured Waves API endpoint. |
| **Both** | Downloads the file AND pushes to the API. |

### Waves API Configuration

*(Visible only when Delivery Mode is "Push to Waves API" or "Both")*

| Field | Description |
|-------|-------------|
| Waves API Endpoint URL | Full URL, e.g. `https://waves.example.com/api/import` |
| Auth Header Name | HTTP header used for auth, default `X-API-Key` |
| API Key | Secret key sent in the auth header (stored encrypted) |
| Timeout (seconds) | Request timeout, default 30 |

---

## DocTypes

### Waves Sync Settings *(Single)*

One global configuration document. Accessible at **Waves Sync Settings** in the desk.

### Waves Sync Log

One document per export/sync run. Auto-named `WS-YY-MM-####`.

Key fields:

| Field | Description |
|-------|-------------|
| Run Datetime | When the run was triggered |
| Triggered By | `Manual`, `API`, `Live Sample Run`, etc. |
| Mode | `Download`, `API Push`, `Both`, `File Upload`, `Test` |
| Status | `Running` → `Success` / `Failed` |
| From Date / To Date | Date range filtered (for ERP exports) |
| Total Invoices | Count of invoices in the payload |
| Total Line Items | Total salesline rows across all invoices |
| JSON File | Attached JSON file (for file upload mode) |
| API Response Code | HTTP status code from Waves API |
| API Response Message | First 140 chars of API response body |
| Error Log | Full error detail if status is Failed |
| JSON Preview | First 2000 chars of the exported JSON |
| Invoice Rows | Child table - one row per invoice with per-invoice status |

### Waves Sync Invoice Row *(Child Table)*

Populated after clicking **Sync Invoices** on a Waves Sync Log.

| Field | Description |
|-------|-------------|
| Invoice Number | As in the JSON file |
| Date | Invoice date (DD/MM/YYYY) |
| Customer | Customer name |
| Amount | Invoice amount (formatted) |
| Outstanding | Outstanding amount (formatted) |
| Items | Number of line items |
| Status | `Pending` / `Success` / `Error` (color coded) |
| Message | Error detail if status is Error |

---

## Usage

### File Upload + Sync (Desk)

This is the primary workflow when you have a Waves JSON file ready.

1. Go to **Waves Sync Log** → **New**
2. In the **Result** section, attach your JSON file in the **JSON File** field
3. **Save** the document
4. A blue **"Sync Invoices"** button appears in the toolbar
5. Click it - the app reads the file, validates the structure, and populates the **Invoice Rows** table
6. Each row is marked **Success** (green) or **Error** (red)
7. If Delivery Mode in Settings is "Push to Waves API" or "Both", the payload is also POSTed to the API

The Invoice Rows table shows every invoice with its status, exactly like Frappe's Data Import view.

### API Export (REST)

Export data directly from ERPNext's Sales Invoices via API.

**Download JSON file:**
```
GET /api/method/waves_sync.api.export.download_waves_json
    ?from_date=2026-01-01&to_date=2026-04-30&company=Your Company
```

Returns a downloadable `.json` file. Also creates a Waves Sync Log entry.

**Get JSON as API response (no download):**
```
GET /api/method/waves_sync.api.export.export_invoices
    ?from_date=2026-01-01&to_date=2026-04-30
```

Returns `{"sales": [...]}` in the response body. No log entry created.

**Full workflow (export + push):**
```
POST /api/method/waves_sync.api.export.run_export
Body: {"from_date": "2026-01-01", "to_date": "2026-04-30", "company": "Your Company"}
```

Exports, pushes to Waves API if configured, creates a log entry, and returns a summary.

All endpoints require Frappe authentication (session cookie or API key/secret header).

### Full Workflow (Desk + API Push)

1. Configure **Waves Sync Settings** with Delivery Mode = "Both" and your Waves API URL
2. Create a new **Waves Sync Log**
3. Either upload a file and click **Sync Invoices**, or use the REST API with `run_export`
4. View the log entry for per-invoice status, API response code, and error details

---

## API Reference

All methods are `@frappe.whitelist()` and accessible via `/api/method/<dotted.path>`.

### `waves_sync.api.export.export_invoices`

Returns the Waves JSON payload without creating a log entry. Useful for quick previews.

**Parameters:**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str | Settings default | Start date `YYYY-MM-DD` |
| `to_date` | str | Settings default | End date `YYYY-MM-DD` |
| `company` | str | Settings default | Company name filter (optional) |

**Returns:** `{"sales": [...]}`

---

### `waves_sync.api.export.download_waves_json`

Streams the payload as a downloadable JSON file and creates a log entry.

**Parameters:** Same as `export_invoices`.

**Returns:** File download (Content-Type: application/json).

---

### `waves_sync.api.export.run_export`

Full export + deliver workflow. Creates a log entry.

**Parameters:** Same as `export_invoices`.

**Returns:**
```json
{
  "log": "WS-26-06-0001",
  "total_invoices": 196,
  "total_line_items": 946,
  "status": "Success"
}
```

---

### `waves_sync.api.sync.process_log_file`

Parses the JSON file attached to a Waves Sync Log and populates the Invoice Rows child table.

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `log_name` | str | Name of the Waves Sync Log document |

**Returns:**
```json
{
  "status": "Success",
  "total_invoices": 196,
  "total_line_items": 946,
  "success_count": 196,
  "error_count": 0,
  "summary": "Processed 196 invoices - 196 succeeded, 0 failed."
}
```

---

## JSON Format

The Waves application expects the following structure:

```json
{
  "sales": [
    {
      "invoice_date": "17/06/2026",
      "invoice_number": "VPA-2627-10351",
      "invoice_amount": "44,457",
      "outstanding_amount": "44,457",
      "sales_order_number": "SO-SF-2627-00062323",
      "sales_person_code": "2210",
      "sales_person_name": "Mahin P S",
      "customer_code": "CU16276",
      "customer_name": "P S A Enterprises",
      "salesline": [
        {
          "product_code": "SGF0060",
          "item_description": "110MM AIZAR VENTCOWL WITH MESH",
          "unit": "NOS",
          "qty": "2",
          "unit_rate": "116.31",
          "tax_base_amount": "101.54",
          "gross_total": "119.82",
          "cd": "10"
        }
      ]
    }
  ]
}
```

### Field Mapping (ERPNext → Waves)

| Waves field | ERPNext source |
|-------------|----------------|
| `invoice_date` | `Sales Invoice.posting_date` (formatted DD/MM/YYYY) |
| `invoice_number` | `Sales Invoice.name` |
| `invoice_amount` | `Sales Invoice.grand_total` (comma-formatted integer) |
| `outstanding_amount` | `Sales Invoice.outstanding_amount` |
| `sales_order_number` | `Sales Invoice Item.sales_order` (first SO linked), falls back to `Sales Invoice.po_no` |
| `sales_person_code` | `Sales Person.custom_sales_person_code` (custom field) |
| `sales_person_name` | `Sales Person.sales_person_name` (first in Sales Team) |
| `customer_code` | `Customer.custom_customer_code` (custom field) |
| `customer_name` | `Sales Invoice.customer_name` |
| `product_code` | `Sales Invoice Item.item_code` |
| `item_description` | `Sales Invoice Item.item_name` |
| `unit` | `Sales Invoice Item.uom` |
| `qty` | `Sales Invoice Item.qty` |
| `unit_rate` | `Sales Invoice Item.rate` (2 decimal places) |
| `tax_base_amount` | `Sales Invoice Item.net_amount` (excl. tax, comma-formatted) |
| `gross_total` | `Sales Invoice Item.amount` (comma-formatted) |
| `cd` | `Sales Invoice Item.discount_percentage` (integer) |

> **Note:** `custom_sales_person_code` and `custom_customer_code` are optional custom fields. If they do not exist on your site, the app falls back to using the document name instead.

---

## Development

### Project Structure

```
waves_sync/
├── pyproject.toml
├── README.md
└── waves_sync/
    ├── __init__.py            # version
    ├── hooks.py               # app wiring: doctype_js, fixtures, CSS
    ├── modules.txt            # "Waves Sync"
    ├── patches.txt
    ├── api/
    │   ├── export.py          # Core export logic + 3 whitelisted endpoints
    │   ├── sync.py            # File upload sync endpoint
    │   └── load_sample.py     # Dev utility: loads sample data for live testing
    ├── public/
    │   ├── js/
    │   │   └── waves_sync_log.js   # "Sync Invoices" button + row coloring
    │   └── css/
    │       └── waves_sync.css      # Row status tint colors
    ├── tests/
    │   ├── test_waves_export.py    # 19 unit tests (format helpers + mocked payload)
    │   └── test_sample_data.py     # 9 integration tests using real sample data
    └── waves_sync/            # module folder
        └── doctype/
            ├── waves_sync_settings/   # Single doctype - global config
            ├── waves_sync_log/        # Per-run audit log
            └── waves_sync_invoice_row/ # Child table - per-invoice status
```

### Running Tests

```bash
# Unit tests only (fast, no DB writes)
bench --site your-site.com run-tests --app waves_sync --module waves_sync.tests.test_waves_export

# Integration tests (creates and cleans up test DB records)
bench --site your-site.com run-tests --app waves_sync --module waves_sync.tests.test_sample_data

# Full test suite
bench --site your-site.com run-tests --app waves_sync
```

### Sample Data

A sample Waves JSON file (196 invoices, 946 line items) is available at `data_ref/sample_inv_data.txt` in the bench root. Use the dev utility to load it for a live test run:

```bash
# Load sample data and run the export
bench --site your-site.com execute waves_sync.api.load_sample.run

# Clean up test records after
bench --site your-site.com execute waves_sync.api.load_sample.cleanup
```

### Building Assets

After editing JS or CSS:

```bash
bench build --app waves_sync
```

### Linting

```bash
cd apps/waves_sync
ruff check .
ruff format .
```

---

## Troubleshooting

**"No file attached" when clicking Sync Invoices**
Save the document after attaching the file before clicking Sync.

**`custom_customer_code` / `custom_sales_person_code` fields empty**
These are optional custom fields. Add them via Frappe's Customize Form if your site has them, then run `bench --site site migrate`.

**API push returns non-200**
Check the `api_response_code` and `api_response_message` fields in the Waves Sync Log, and the `Error Log` section for the full response body.

**Amounts look rounded (e.g. "102" instead of "101.54")**
`invoice_amount` and `outstanding_amount` are formatted as comma-separated integers (Waves specification). `unit_rate` retains 2 decimal places.

---

## License

MIT - Faircode Infotech, nakul@faircodetech.com
