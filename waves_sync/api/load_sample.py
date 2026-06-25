"""
Load sample invoice data from data_ref/sample_inv_data.txt into kalyan.com,
run the Waves export, create a Waves Sync Log entry, and print a summary.

Usage:
    bench --site kalyan.com execute waves_sync.api.load_sample.run
    bench --site kalyan.com execute waves_sync.api.load_sample.cleanup
"""

import json
import os

import frappe
from frappe.utils import now_datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))
SAMPLE_DATA_PATH = os.path.join(BENCH_ROOT, "data_ref", "sample_inv_data.txt")
TEST_DATE = "2026-06-17"
TEST_PREFIX = "TEST-WAVES-"
SAMPLE_UOMS = {"LTR", "ROL", "NOS", "MTR"}


def _load_json():
	with open(SAMPLE_DATA_PATH, encoding="utf-8", errors="replace") as f:
		return json.load(f)


def _setup_masters(sales):
	print("Setting up master data...")

	for uom_name in SAMPLE_UOMS:
		if not frappe.db.exists("UOM", uom_name):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom_name}).insert(ignore_permissions=True)
			print(f"  Created UOM: {uom_name}")

	customers, items = {}, {}
	for inv in sales:
		cust_key = TEST_PREFIX + inv["customer_code"]
		customers[cust_key] = inv["customer_name"].strip()
		for line in inv.get("salesline", []):
			item_key = TEST_PREFIX + line["product_code"]
			items[item_key] = {"desc": line.get("item_description", "")[:140], "uom": line.get("unit", "NOS")}

	cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
	terr = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"

	created_customers = 0
	for cust_key, cust_name in customers.items():
		if not frappe.db.exists("Customer", cust_key):
			frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cust_key,
				"customer_type": "Company",
				"customer_group": cg,
				"territory": terr,
			}).insert(ignore_permissions=True)
			try:
				frappe.db.set_value("Customer", cust_key, "custom_customer_code", cust_key.replace(TEST_PREFIX, ""))
			except Exception:
				pass
			created_customers += 1

	ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
	created_items = 0
	for item_key, info in items.items():
		if not frappe.db.exists("Item", item_key):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": item_key,
				"item_name": info["desc"] or item_key,
				"item_group": ig,
				"stock_uom": info["uom"],
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)
			created_items += 1

	frappe.db.commit()
	print(f"  Created {created_customers} customers, {created_items} items")


def _insert_invoices(sales):
	print(f"Inserting {len(sales)} invoices...")

	company = (frappe.get_all("Company", fields=["name"], limit=1) or [{"name": "Test Company"}])[0]["name"]
	now_str = str(now_datetime())
	cust_map = {inv["customer_code"]: TEST_PREFIX + inv["customer_code"] for inv in sales}

	inserted = 0
	total_lines = 0
	for idx, inv in enumerate(sales):
		inv_name = TEST_PREFIX + inv["invoice_number"]
		if frappe.db.exists("Sales Invoice", inv_name):
			continue

		grand_total = float(inv["invoice_amount"].replace(",", ""))
		outstanding = float(inv["outstanding_amount"].replace(",", ""))
		cust_key = cust_map[inv["customer_code"]]

		frappe.db.sql(
			"""
			INSERT INTO `tabSales Invoice`
				(name, docstatus, posting_date, customer, customer_name,
				 grand_total, outstanding_amount, po_no, company,
				 creation, modified, owner, modified_by, idx)
			VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Administrator', 'Administrator', %s)
			""",
			(
				inv_name, TEST_DATE, cust_key, inv["customer_name"].strip()[:140],
				grand_total, outstanding,
				inv.get("sales_order_number") or "", company,
				now_str, now_str, idx + 1,
			),
		)
		inserted += 1

		for li, line in enumerate(inv.get("salesline", [])):
			item_key = TEST_PREFIX + line["product_code"]
			qty = float(str(line.get("qty", 1)).replace(",", ""))
			rate = float(str(line.get("unit_rate", 0)).replace(",", ""))
			amount = float(str(line.get("gross_total", 0)).replace(",", ""))
			net_amount = float(str(line.get("tax_base_amount", 0)).replace(",", ""))

			frappe.db.sql(
				"""
				INSERT INTO `tabSales Invoice Item`
					(name, parent, parenttype, parentfield, docstatus,
					 item_code, item_name, description, uom, qty, rate,
					 amount, net_amount, discount_percentage,
					 creation, modified, owner, modified_by, idx)
				VALUES (%s, %s, 'Sales Invoice', 'items', 1,
					%s, %s, %s, %s, %s, %s, %s, %s, 0,
					%s, %s, 'Administrator', 'Administrator', %s)
				""",
				(
					f"{inv_name}-{li+1}", inv_name,
					item_key, line.get("item_description", "")[:140], line.get("item_description", "")[:140],
					line.get("unit", "NOS"), qty, rate, amount, net_amount,
					now_str, now_str, li + 1,
				),
			)
			total_lines += 1

	frappe.db.commit()
	print(f"  Inserted {inserted} invoices, {total_lines} line items")
	return inserted


def run():
	"""Load sample data and run the Waves export. Creates a Waves Sync Log entry."""
	print(f"\n{'='*60}")
	print("Waves Sync - Live Sample Data Run")
	print(f"{'='*60}")
	print(f"Sample file: {SAMPLE_DATA_PATH}")

	data = _load_json()
	sales = data.get("sales", [])
	print(f"Sample file contains: {len(sales)} invoices")

	_setup_masters(sales)
	inserted = _insert_invoices(sales)

	print("\nRunning Waves export...")
	from waves_sync.api.export import _build_waves_payload, _create_log

	payload, total_inv, total_items = _build_waves_payload(
		TEST_DATE, TEST_DATE, company=None, include_zero_outstanding=False
	)

	# Filter to our test invoices only
	test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]

	log = _create_log("Download", TEST_DATE, TEST_DATE, "", triggered_by="Live Sample Run")
	json_str = json.dumps({"sales": test_sales}, ensure_ascii=False, indent=2)
	log.total_invoices = len(test_sales)
	log.total_line_items = sum(len(s.get("salesline", [])) for s in test_sales)
	log.json_preview = json_str[:2000]
	log.status = "Success"
	log.save(ignore_permissions=True)
	frappe.db.commit()

	print(f"\n{'='*60}")
	print("RESULT")
	print(f"{'='*60}")
	print(f"Waves Sync Log: {log.name}")
	print(f"Total invoices exported: {log.total_invoices}")
	print(f"Total line items exported: {log.total_line_items}")
	print(f"\nSample of first invoice:")
	first = test_sales[0] if test_sales else {}
	print(json.dumps(first, indent=2, ensure_ascii=False))
	print(f"\nFull JSON preview (first 500 chars):")
	print(json_str[:500])
	print(f"\nView the log in Frappe Desk: /app/waves-sync-log/{log.name}")


def cleanup():
	"""Remove all TEST-WAVES-* records from the DB."""
	print("Cleaning up test records...")

	names = frappe.db.sql(
		"SELECT name FROM `tabSales Invoice` WHERE name LIKE %s",
		(TEST_PREFIX + "%",),
		as_list=True,
	)
	inv_names = [r[0] for r in names]

	if inv_names:
		ph = ", ".join(["%s"] * len(inv_names))
		frappe.db.sql(f"DELETE FROM `tabSales Invoice Item` WHERE parent IN ({ph})", inv_names)
		frappe.db.sql(f"DELETE FROM `tabSales Invoice` WHERE name IN ({ph})", inv_names)
		print(f"  Deleted {len(inv_names)} invoices")

	for doctype, prefix in [("Customer", TEST_PREFIX), ("Item", TEST_PREFIX)]:
		records = frappe.db.sql(
			f"SELECT name FROM `tab{doctype}` WHERE name LIKE %s",
			(prefix + "%",),
			as_list=True,
		)
		for r in records:
			try:
				frappe.delete_doc(doctype, r[0], ignore_permissions=True, force=True)
			except Exception:
				pass
		if records:
			print(f"  Deleted {len(records)} {doctype} records")

	frappe.db.commit()
	print("Done.")
