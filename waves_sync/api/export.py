import frappe
from frappe import _
from frappe.utils import flt, now_datetime
import json
import requests
from collections import defaultdict


def _fmt_amount(value):
	return "{:,.0f}".format(flt(value))


def _fmt_date(d):
	if not d:
		return ""
	return d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)


def _fmt_qty(value):
	v = flt(value)
	return str(int(v)) if v == int(v) else str(v)


def _fmt_rate(value):
	return "{:.2f}".format(flt(value))


def _fmt_cd(discount_pct):
	return str(int(flt(discount_pct)))


def _build_waves_payload(from_date, to_date, company=None, include_zero_outstanding=False):
	"""Core logic: query ERPNext and build Waves-compatible JSON payload."""
	filters = {
		"docstatus": 1,
		"posting_date": ["between", [from_date, to_date]],
	}
	if not include_zero_outstanding:
		filters["outstanding_amount"] = [">", 0]
	if company:
		filters["company"] = company

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name", "posting_date", "customer", "customer_name",
			"grand_total", "outstanding_amount", "po_no",
		],
		order_by="posting_date asc, name asc",
	)

	if not invoices:
		return {"sales": []}, 0, 0

	invoice_names = [inv["name"] for inv in invoices]

	# Sales team (first sales person per invoice)
	sales_team_rows = frappe.get_all(
		"Sales Team",
		filters={"parent": ["in", invoice_names], "parenttype": "Sales Invoice"},
		fields=["parent", "sales_person"],
		order_by="idx asc",
	)
	sales_person_map = {}
	for row in sales_team_rows:
		if row["parent"] not in sales_person_map:
			sales_person_map[row["parent"]] = row["sales_person"]

	all_sp_names = list(set(sales_person_map.values()))
	sp_code_map = {}
	if all_sp_names:
		sp_fields = ["name", "sales_person_name"]
		if frappe.db.has_column("Sales Person", "custom_sales_person_code"):
			sp_fields.append("custom_sales_person_code")
		sp_records = frappe.get_all(
			"Sales Person",
			filters={"name": ["in", all_sp_names]},
			fields=sp_fields,
		)
		for sp in sp_records:
			sp_code_map[sp["name"]] = {
				"code": sp.get("custom_sales_person_code") or sp["name"],
				"full_name": sp.get("sales_person_name") or sp["name"],
			}

	# Line items
	items = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", invoice_names]},
		fields=[
			"parent", "item_code", "item_name", "description",
			"uom", "qty", "rate", "net_amount", "amount", "discount_percentage",
		],
		order_by="parent asc, idx asc",
	)
	items_by_invoice = defaultdict(list)
	total_line_items = 0
	for item in items:
		items_by_invoice[item["parent"]].append(item)
		total_line_items += 1

	# Sales order links
	so_rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", invoice_names], "sales_order": ["!=", ""]},
		fields=["parent", "sales_order"],
		order_by="parent asc, idx asc",
	)
	so_map = {}
	for row in so_rows:
		if row["parent"] not in so_map and row.get("sales_order"):
			so_map[row["parent"]] = row["sales_order"]

	# Customer codes
	all_customers = list({inv["customer"] for inv in invoices})
	cust_fields = ["name", "customer_name"]
	if frappe.db.has_column("Customer", "custom_customer_code"):
		cust_fields.append("custom_customer_code")
	cust_records = frappe.get_all(
		"Customer",
		filters={"name": ["in", all_customers]},
		fields=cust_fields,
	)
	cust_code_map = {
		c["name"]: c.get("custom_customer_code") or c["name"]
		for c in cust_records
	}

	sales_list = []
	for inv in invoices:
		inv_name = inv["name"]
		sp_name = sales_person_map.get(inv_name, "")
		sp_info = sp_code_map.get(sp_name, {"code": "", "full_name": ""})

		salesline = []
		for item in items_by_invoice.get(inv_name, []):
			salesline.append({
				"product_code": item["item_code"],
				"item_description": (item.get("item_name") or item.get("description") or "").strip(),
				"unit": item["uom"],
				"qty": _fmt_qty(item["qty"]),
				"unit_rate": _fmt_rate(item["rate"]),
				"tax_base_amount": _fmt_amount(item["net_amount"]),
				"gross_total": _fmt_amount(item["amount"]),
				"cd": _fmt_cd(item.get("discount_percentage") or 0),
			})

		sales_list.append({
			"invoice_date": _fmt_date(inv["posting_date"]),
			"invoice_number": inv_name,
			"invoice_amount": _fmt_amount(inv["grand_total"]),
			"outstanding_amount": _fmt_amount(inv["outstanding_amount"]),
			"sales_order_number": so_map.get(inv_name) or inv.get("po_no") or "",
			"sales_person_code": sp_info["code"],
			"sales_person_name": sp_info["full_name"],
			"customer_code": cust_code_map.get(inv["customer"], ""),
			"customer_name": inv["customer_name"].strip(),
			"salesline": salesline,
		})

	return {"sales": sales_list}, len(invoices), total_line_items


def _create_log(mode, from_date, to_date, company, triggered_by="Manual"):
	log = frappe.new_doc("Waves Sync Log")
	log.run_datetime = now_datetime()
	log.triggered_by = triggered_by
	log.mode = mode
	log.from_date = from_date
	log.to_date = to_date
	log.company = company or ""
	log.status = "Running"
	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log


def _push_to_api(payload, settings):
	"""POST JSON payload to Waves API. Returns (status_code, message)."""
	headers = {
		"Content-Type": "application/json",
		settings.api_auth_header or "X-API-Key": settings.get_password("waves_api_key") or "",
	}
	timeout = settings.api_timeout or 30
	try:
		resp = requests.post(
			settings.waves_api_url,
			data=json.dumps(payload, ensure_ascii=False),
			headers=headers,
			timeout=timeout,
		)
		return resp.status_code, resp.text[:500]
	except Exception as e:
		return 0, str(e)


@frappe.whitelist()
def export_invoices(from_date=None, to_date=None, company=None):
	"""
	Returns Waves-compatible JSON for submitted Sales Invoices in date range.
	Does not create a log entry (use run_export for full workflow).
	"""
	settings = frappe.get_single("Waves Sync Settings")
	from_date = from_date or settings.default_from_date
	to_date = to_date or settings.default_to_date
	company = company or settings.default_company

	if not from_date or not to_date:
		frappe.throw(_("from_date and to_date are required."))

	include_zero = bool(settings.include_zero_outstanding)
	payload, _, _ = _build_waves_payload(from_date, to_date, company, include_zero)
	return payload


@frappe.whitelist()
def download_waves_json(from_date=None, to_date=None, company=None):
	"""Streams the Waves JSON as a downloadable file."""
	settings = frappe.get_single("Waves Sync Settings")
	from_date = from_date or settings.default_from_date
	to_date = to_date or settings.default_to_date
	company = company or settings.default_company

	if not from_date or not to_date:
		frappe.throw(_("from_date and to_date are required."))

	include_zero = bool(settings.include_zero_outstanding)
	payload, total_inv, total_items = _build_waves_payload(from_date, to_date, company, include_zero)

	log = _create_log("Download", from_date, to_date, company, triggered_by="API")
	try:
		json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
		log.total_invoices = total_inv
		log.total_line_items = total_items
		log.json_preview = json_bytes.decode("utf-8")[:2000]
		log.status = "Success"
		log.save(ignore_permissions=True)
		frappe.db.commit()

		filename = f"waves_invoices_{from_date}_to_{to_date}.json"
		frappe.response["filename"] = filename
		frappe.response["filecontent"] = json_bytes
		frappe.response["type"] = "download"
		frappe.response["content_type"] = "application/json"
	except Exception as e:
		log.status = "Failed"
		log.error_log = str(e)
		log.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(str(e))


@frappe.whitelist()
def run_export(from_date=None, to_date=None, company=None):
	"""
	Full workflow: export → deliver per delivery_mode → log.
	Returns log name and summary.
	"""
	settings = frappe.get_single("Waves Sync Settings")
	from_date = from_date or settings.default_from_date
	to_date = to_date or settings.default_to_date
	company = company or settings.default_company

	if not from_date or not to_date:
		frappe.throw(_("from_date and to_date are required."))

	mode_map = {
		"Download JSON": "Download",
		"Push to Waves API": "API Push",
		"Both": "Both",
	}
	mode = mode_map.get(settings.delivery_mode, "Download")
	log = _create_log(mode, from_date, to_date, company, triggered_by="Manual")

	try:
		include_zero = bool(settings.include_zero_outstanding)
		payload, total_inv, total_items = _build_waves_payload(from_date, to_date, company, include_zero)

		log.total_invoices = total_inv
		log.total_line_items = total_items
		json_str = json.dumps(payload, ensure_ascii=False, indent=2)
		log.json_preview = json_str[:2000]

		if mode in ("API Push", "Both"):
			if not settings.waves_api_url:
				frappe.throw(_("Waves API URL is not configured in Waves Sync Settings."))
			code, msg = _push_to_api(payload, settings)
			log.api_response_code = code
			log.api_response_message = msg
			if code not in (200, 201, 202):
				log.status = "Failed"
				log.error_log = f"HTTP {code}: {msg}"
				log.save(ignore_permissions=True)
				frappe.db.commit()
				frappe.throw(_(f"Waves API returned HTTP {code}: {msg}"))

		log.status = "Success"
		log.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"log": log.name,
			"total_invoices": total_inv,
			"total_line_items": total_items,
			"status": "Success",
		}
	except Exception as e:
		log.status = "Failed"
		log.error_log = str(e)
		log.save(ignore_permissions=True)
		frappe.db.commit()
		raise
