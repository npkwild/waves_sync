import json

import frappe
from frappe import _


def _read_file_content(file_url):
	"""Return file content as string, handling both public and private files."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	content = file_doc.get_content()
	if isinstance(content, bytes):
		content = content.decode("utf-8", errors="replace")
	return content


def _parse_waves_json(content):
	"""Parse and basic-validate the JSON content. Returns sales list."""
	try:
		data = json.loads(content)
	except json.JSONDecodeError as e:
		frappe.throw(_(f"Invalid JSON file: {e}"))

	if not isinstance(data, dict) or "sales" not in data:
		frappe.throw(_("File must be a JSON object with a top-level \"sales\" array."))

	sales = data["sales"]
	if not isinstance(sales, list):
		frappe.throw(_("\"sales\" must be an array."))

	return sales, data


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
	sales, full_payload = _parse_waves_json(content)

	if not sales:
		frappe.throw(_("No invoices found in the JSON file."))

	# Clear existing rows and rebuild
	log.invoice_rows = []
	for inv in sales:
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

	log.total_invoices = len(sales)
	log.total_line_items = sum(len(inv.get("salesline", [])) for inv in sales)
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
		# Download / review only — mark all rows as Success (file parsed OK)
		for row in log.invoice_rows:
			row.status = "Success"
		log.status = "Success"

	log.save(ignore_permissions=True)
	frappe.db.commit()

	success_count = sum(1 for r in log.invoice_rows if r.status == "Success")
	error_count = sum(1 for r in log.invoice_rows if r.status == "Error")

	return {
		"status": log.status,
		"total_invoices": log.total_invoices,
		"total_line_items": log.total_line_items,
		"success_count": success_count,
		"error_count": error_count,
		"summary": (
			f"Processed {log.total_invoices} invoices — "
			f"{success_count} succeeded, {error_count} failed."
		),
	}
