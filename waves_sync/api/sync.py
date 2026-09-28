import json

import frappe
from frappe import _
from frappe.model.naming import make_autoname



def _read_file_content(file_url):
	"""Return file content as string, handling both public and private files."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	content = file_doc.get_content()
	if isinstance(content, bytes):
		content = content.replace(b"\xff", b" ").decode("utf-8", errors="replace")
	return content


def _parse_waves_json(content):
	"""Parse and basic-validate the JSON content. Returns sales list."""
	try:
		data = json.loads(content)
	except json.JSONDecodeError as e:
		frappe.throw(_(f"Invalid JSON file: {e}"))

	if not isinstance(data, dict):
		frappe.throw(_("File must be a JSON object."))

	if "sales" in data:
		rows, file_type = data["sales"], "Sales"
	elif "Customer Payments" in data:
		rows, file_type = data["Customer Payments"], "Collection"
	else:
		frappe.throw(_("File must have a top-level \"sales\" or \"Customer Payments\" array."))

	if not isinstance(rows, list):
		frappe.throw(_("The records key must be an array."))

	return rows, file_type, data

from datetime import datetime


def _to_num(v):
	try:
		return float(str(v).replace(",", "").strip() or 0)
	except (ValueError, TypeError):
		return 0.0


def _parse_inv_date(s):
	s = str(s or "").strip()
	for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
		try:
			return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
		except ValueError:
			continue
	return frappe.utils.nowdate()


def _resolve_customer(inv):
	code = (inv.get("customer_code") or "").strip()
	name = (inv.get("customer_name") or "").strip()
	if code and frappe.db.has_column("Customer", "custom_customer_code"):
		c = frappe.db.get_value("Customer", {"custom_customer_code": code}, "name")
		if c:
			return c
	if name:
		c = frappe.db.get_value("Customer", {"customer_name": name}, "name")
		if c:
			return c
		if frappe.db.exists("Customer", name):
			return name
	return None


def _resolve_item(code):
	code = (code or "").strip()
	return code if code and frappe.db.exists("Item", code) else None

def _get_sales_tax_template(company):
	return (
		frappe.db.get_value("Sales Taxes and Charges Template",
			{"company": company, "is_default": 1}, "name")
		or frappe.db.get_value("Sales Taxes and Charges Template",
			{"company": company, "name": ["like", "Output GST In-state%"]}, "name")
	)

def _create_one_invoice(inv, company):
	invoice_number = (inv.get("invoice_number") or "").strip()

	if invoice_number: 
		# already tagged → skip
		if frappe.db.exists("Sales Invoice", {"custom_waves_invoice_number": invoice_number}):
			return "skipped", f"{invoice_number}: already imported"
		# exists by name (backup invoice) but untagged → tag it so payments can match
		if frappe.db.exists("Sales Invoice", invoice_number):
			frappe.db.set_value("Sales Invoice", invoice_number,
				"custom_waves_invoice_number", invoice_number)
			frappe.db.commit()
			return "linked", f"{invoice_number}: tagged existing invoice"

	customer = _resolve_customer(inv)
	if not customer:
		who = inv.get("customer_code") or inv.get("customer_name") or "?"
		return "skipped", f"{invoice_number}: customer not found ({who})"

	items, missing = [], []
	for line in inv.get("salesline", []):
		item_code = _resolve_item(line.get("product_code"))
		if not item_code:
			missing.append(line.get("product_code") or "?")
			continue
		qty = _to_num(line.get("qty")) or 1
		taxable = _to_num(line.get("tax_base_amount"))   # per-line net (discount already applied)
		qty = _to_num(line.get("qty")) or 1
		taxable = _to_num(line.get("tax_base_amount"))   # per-line net (discount already applied)
		row = {
			"item_code": item_code,
			"qty": qty,
			"rate": taxable / qty if qty else taxable,     # per-unit net → +18% = gross_total
		}
		uom = (line.get("unit") or "").strip()
		if uom and frappe.db.exists("UOM", uom):
			row["uom"] = uom
		items.append(row)

	if not items:
		return "skipped", f"{invoice_number}: no valid items"

	try:
		doc = frappe.new_doc("Sales Invoice")
		doc.customer = customer
		doc.company = company
		doc.set_posting_time = 1
		doc.posting_date = _parse_inv_date(inv.get("invoice_date"))
		doc.custom_waves_invoice_number = invoice_number
		doc.po_no = inv.get("sales_order_number") or ""
		for row in items:
			doc.append("items", row)

				# let ERPNext + india_compliance set the correct GST template & taxes
		sales_person_name = (inv.get("sales_person_name") or "").strip()

		if sales_person_name:
			doc.append("sales_team", {
				"sales_person": sales_person_name,
				"allocated_percentage": 100
			})
		doc.run_method("set_missing_values")
		doc.run_method("set_taxes")
		doc.run_method("calculate_taxes_and_totals")

		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return "skipped", f"{invoice_number}: draft insert failed: {str(e)[:150]}"

	if missing:
		return "draft", f"{invoice_number}: {doc.name} (missing items: {', '.join(missing)})"

	try:
		doc.flags.ignore_permissions = True
		doc.submit()
		frappe.db.commit()
		return "submitted", doc.name
	except Exception as e:
		frappe.db.rollback()
		return "draft", f"{invoice_number}: {doc.name} (submit failed: {str(e)[:150]})"

@frappe.whitelist()
def process_log_file(log_name):
	"""
	Parse the JSON file attached to a Waves Sync Log and populate the invoice rows table.
	If the Waves API is configured and delivery mode includes API push, push the payload too.
	"""
	log = frappe.get_doc("Waves Sync Log", log_name)

	if not log.file_attachment:
		frappe.throw(_("No file attached. Upload a Waves JSON file and save first."))

	content = _read_file_content(log.file_attachment)
	records, file_type, full_payload = _parse_waves_json(content)

	if not records:
		frappe.throw(_("No records found in the JSON file."))

	# Clear existing rows and rebuild
	log.invoice_rows = []
	if file_type == "Sales":
		for inv in records:
			log.append("invoice_rows", {
				"invoice_number": inv.get("invoice_number", ""),
				"invoice_date": inv.get("invoice_date", ""),
				"customer_name": inv.get("customer_name", ""),
				"invoice_amount": inv.get("invoice_amount", ""),
				"outstanding_amount": inv.get("outstanding_amount", ""),
				"items_count": len(inv.get("salesline", [])),
				"status": "Pending",
				"message": "",
			})
		log.total_line_items = sum(len(inv.get("salesline", [])) for inv in records)
	else:  # Collection
		MAX_ROWS = 500   # cap — 17k rows will time out
		for pay in records[:MAX_ROWS]:
			log.append("invoice_rows", {
				"invoice_number": pay.get("invoice_number", ""),
				"invoice_date": pay.get("date", ""),
				"customer_name": pay.get("dealer_code", ""),
				"invoice_amount": pay.get("amount", ""),
				"outstanding_amount": pay.get("outstanding_amount", ""),
				"items_count": 0,
				"status": "Pending",
				"message": "",
			})
		log.total_line_items = 0

	log.total_invoices = len(records)
	log.mode = "File Upload"
	log.triggered_by = "Manual"

	# Push to Waves API if configured
	settings = frappe.get_single("Waves Sync Settings")
	delivery_mode = settings.delivery_mode or "Download JSON"

	if delivery_mode in ("Push to Waves API", "Both") and settings.waves_api_url:
		from waves_sync.api.export import _push_to_api

		log.status = "Running"
		log.save(ignore_permissions=True)
		frappe.db.commit()

		code, msg = _push_to_api(full_payload, settings)
		row_status = "Success" if code in (200, 201, 202) else "Error"
		row_message = "" if row_status == "Success" else f"HTTP {code}: {msg[:200]}"

		for row in log.invoice_rows:
			row.status = row_status
			row.message = row_message

		log.status = "Success" if row_status == "Success" else "Failed"
		log.api_response_code = code
		log.api_response_message = (msg or "")[:140]
		if row_message:
			log.error_log = row_message
	else:
		for row in log.invoice_rows:
			row.status = "Success"
		log.status = "Success"

	log.save(ignore_permissions=True)
	frappe.db.commit()

	success_count = sum(1 for r in log.invoice_rows if r.status == "Success")
	error_count = sum(1 for r in log.invoice_rows if r.status == "Error")

	# Resolve company once
	company = (
		settings.default_company
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)

	create_result = None


	result = {
		"status": log.status,
		"total_invoices": log.total_invoices,
		"total_line_items": log.total_line_items,
		"success_count": success_count,
		"error_count": error_count,
		"summary": (
			f"Processed {log.total_invoices} records — "
			f"{success_count} succeeded, {error_count} failed."
		),
	}
	if create_result and "error" not in create_result:
		if create_result["type"] == "invoices":
			result["summary"] += (
				f" Sales Invoices: {create_result['submitted']} submitted, "
				f"{create_result['drafted']} draft, {create_result['skipped']} skipped."
			)
		else:  # payments queued in background
			result["summary"] += " Payment creation started in the background — refresh in a minute to see results."
	if create_result:
		result["created"] = create_result
	return result

def _create_one_payment(pay, company):
	invoice_number = (pay.get("invoice_number") or "").strip()
	amount = _to_num(pay.get("amount"))
	receipt = (pay.get("receipt_no") or "").strip()

	if not invoice_number or amount <= 0:
		return "skipped", f"{invoice_number}: no invoice/amount"

	# Find the submitted Sales Invoice by the Waves number
	si_name = None
	if frappe.db.has_column("Sales Invoice", "custom_waves_invoice_number"):
		si_name = frappe.db.get_value(
			"Sales Invoice",
			{"custom_waves_invoice_number": invoice_number, "docstatus": 1},
			"name",
		)
	if not si_name:
		return "skipped", f"{invoice_number}: submitted invoice not found"

	# Duplicate guard: same receipt AND same invoice already posted
	if receipt:
		existing = frappe.db.sql(
			"""
			SELECT per.parent
			FROM `tabPayment Entry Reference` per
			JOIN `tabPayment Entry` pe ON pe.name = per.parent
			WHERE pe.reference_no = %s
			  AND per.reference_name = %s
			  AND pe.docstatus = 1
			LIMIT 1
			""",
			(receipt, si_name),
		)
		if existing:
			return "skipped", f"{invoice_number}: receipt {receipt} already applied"

	# Check outstanding BEFORE building the PE — skip+log on overpayment
	outstanding = _to_num(
		frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount")
	)
	if amount > outstanding:
		return "skipped", (
			f"{invoice_number}: payment {amount} exceeds outstanding "
			f"{outstanding} (receipt {receipt}) — manual review"
		)

	try:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		pe = get_payment_entry("Sales Invoice", si_name)
		pe.reference_no = receipt or "WAVES"
		pe.reference_date = _parse_inv_date(pay.get("date"))
		pe.paid_amount = amount
		pe.received_amount = amount
		for ref in pe.references:
			ref.allocated_amount = amount
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		frappe.db.commit()
		return "submitted", pe.name
	except Exception as e:
		frappe.db.rollback()
		msg = str(e)[:150]
		# closed-period and similar submit failures land here → skip + log
		return "skipped", f"{invoice_number}: {msg}"

def si_autoname(doc, method=None):
    """Waves-imported invoices are named after their Waves number;
    manual invoices fall back to the standard naming series."""
    waves_no = (getattr(doc, "custom_waves_invoice_number", None) or "").strip()
    if waves_no:
        doc.name = waves_no
    else:
        doc.name = make_autoname(doc.naming_series or "ACC-SINV-.YYYY.-")
	
def _run_payment_creation(log_name):
	"""Background job: create Payment Entries for every collection row in the log's file."""
	log = frappe.get_doc("Waves Sync Log", log_name)
	content = _read_file_content(log.file_attachment)
	records, file_type, _p = _parse_waves_json(content)
	if file_type != "Collection":
		return

	company = (
		frappe.get_single("Waves Sync Settings").default_company
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)

	submitted, skipped, messages = 0, 0, []
	for i, pay in enumerate(records):
		outcome, msg = _create_one_payment(pay, company)
		if outcome == "submitted":
			submitted += 1
		else:
			skipped += 1
			if len(messages) < 500:
				messages.append(msg)
		# periodic progress write so you can watch it
		if (i + 1) % 200 == 0:
			frappe.db.set_value(
				"Waves Sync Log", log_name,
				"error_log",
				f"Progress: {i+1}/{len(records)} — {submitted} posted, {skipped} skipped",
				update_modified=False,
			)
			frappe.db.commit()

	log = frappe.get_doc("Waves Sync Log", log_name)
	log.status = "Success" if skipped == 0 else "Partial"
	log.error_log = (
		f"Payments: {submitted} submitted, {skipped} skipped.\n\n"
		+ "\n".join(messages)
	)[:100000]
	log.save(ignore_permissions=True)
	frappe.db.commit()
	
@frappe.whitelist()
def create_payments(log_name):
	"""Kick off Payment Entry creation as a background job."""
	frappe.db.set_value("Waves Sync Log", log_name, "status", "Running")
	frappe.db.commit()
	frappe.enqueue(
		"waves_sync.api.sync._run_payment_creation",
		queue="long",
		timeout=3600,
		log_name=log_name,
	)
	return {"queued": True, "message": "Payment creation started in the background. Refresh in a minute to see results."}


@frappe.whitelist()
def create_records(log_name):
	"""Create Sales Invoices (sales file) or Payment Entries (collection file).
	Idempotent: re-running retries only the not-yet-created records.
	Writes a created/failed report to the log's error_log."""
	log = frappe.get_doc("Waves Sync Log", log_name)
	if not log.file_attachment:
		frappe.throw(_("No file attached."))

	records, file_type, _p = _parse_waves_json(_read_file_content(log.file_attachment))

	company = (
		frappe.get_single("Waves Sync Settings").default_company
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if not company:
		frappe.throw(_("No company configured."))

	if file_type == "Sales":
		created, failed, fail_lines = 0, 0, []
		for inv in records:
			outcome, msg = _create_one_invoice(inv, company)
			if outcome in ("submitted", "draft", "linked"):
				created += 1
			elif outcome == "skipped" and "already imported" in msg:
				created += 1   # already exists = counts as created
			else:
				failed += 1
				fail_lines.append(msg)

		total = len(records)
		report = (
			f"Run at {frappe.utils.now()}\n"
			f"Total: {total} | Created: {created} | Not created: {failed}\n"
			f"{'-'*40}\n"
			+ ("\n".join(fail_lines) if fail_lines else "All records created.")
		)
		log.reload()
		log.error_log = report[:100000]
		log.status = "Success" if failed == 0 else "Partial"
		log.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"summary": f"Created {created}/{total}. Not created: {failed}. See Error Log for details.",
		}

	# Collection -> background job (writes its own report to error_log)
	frappe.db.set_value("Waves Sync Log", log_name, "status", "Running")
	frappe.db.commit()
	frappe.enqueue(
		"waves_sync.api.sync._run_payment_creation",
		queue="long", timeout=3600, log_name=log_name,
	)
	return {"summary": "Payment creation started in the background — refresh in a minute for the report."}