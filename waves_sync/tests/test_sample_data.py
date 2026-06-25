"""
Integration test: loads sample invoice data from data_ref/sample_inv_data.txt,
inserts test records directly into the DB, calls _build_waves_payload,
and validates the output structure and counts.

All test records are prefixed "TEST-WAVES-" for easy identification and cleanup.
"""

import json
import os
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime, today

SAMPLE_DATA_PATH = os.path.join(
	os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))),
	"data_ref",
	"sample_inv_data.txt",
)
TEST_DATE = "2026-06-17"
TEST_PREFIX = "TEST-WAVES-"
TEST_COMPANY = "Test Waves Company"

# Unique UOMs in the sample data
SAMPLE_UOMS = {"LTR", "ROL", "NOS", "MTR"}


def _load_sample_data():
	with open(SAMPLE_DATA_PATH, encoding="utf-8", errors="replace") as f:
		return json.load(f)


class TestSampleDataExport(IntegrationTestCase):
	"""
	Creates minimal test masters and raw DB invoice records from sample_inv_data.txt,
	then validates that _build_waves_payload returns the correct invoice and line item counts.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.data = _load_sample_data()
		cls.sales = cls.data.get("sales", [])
		cls._created_customers = []
		cls._created_items = []
		cls._created_uoms = []
		cls._created_sales_persons = []
		cls._inserted_invoice_names = []
		cls._inserted_item_parents = []

		# Determine a usable company
		existing_companies = frappe.get_all("Company", fields=["name"], limit=1)
		cls.company = existing_companies[0]["name"] if existing_companies else None

		cls._setup_masters()
		cls._insert_test_invoices()

	@classmethod
	def _setup_masters(cls):
		"""Create UOMs, Customers, Items, and Sales Persons needed by test invoices."""
		# UOMs
		for uom_name in SAMPLE_UOMS:
			if not frappe.db.exists("UOM", uom_name):
				uom = frappe.get_doc({"doctype": "UOM", "uom_name": uom_name})
				uom.insert(ignore_permissions=True)
				cls._created_uoms.append(uom_name)

		# Collect unique entities from sample data
		customers = {}  # code -> name
		items = {}  # code -> description + uom
		sales_persons = {}  # code -> name

		for inv in cls.sales:
			cust_key = TEST_PREFIX + inv["customer_code"]
			customers[cust_key] = {
				"code": inv["customer_code"],
				"name": inv["customer_name"].strip(),
			}
			sp_code = inv["sales_person_code"]
			sp_key = TEST_PREFIX + sp_code
			sales_persons[sp_key] = {
				"code": sp_code,
				"full_name": inv["sales_person_name"].strip(),
			}
			for line in inv.get("salesline", []):
				item_key = TEST_PREFIX + line["product_code"]
				items[item_key] = {
					"code": line["product_code"],
					"description": line.get("item_description", ""),
					"uom": line.get("unit", "NOS"),
				}

		# Create Customers
		for cust_key, cust_info in customers.items():
			if not frappe.db.exists("Customer", cust_key):
				cust = frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": cust_key,
						"customer_type": "Company",
						"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
						or "All Customer Groups",
						"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
					}
				)
				cust.insert(ignore_permissions=True)
				# Set custom_customer_code via db.set_value (field may not exist; ignore error)
				try:
					frappe.db.set_value("Customer", cust_key, "custom_customer_code", cust_info["code"])
				except Exception:
					pass
				cls._created_customers.append(cust_key)

		# Create Items
		for item_key, item_info in items.items():
			if not frappe.db.exists("Item", item_key):
				item = frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item_key,
						"item_name": item_info["description"][:140] or item_key,
						"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
						"stock_uom": item_info["uom"],
						"is_stock_item": 0,
					}
				)
				item.insert(ignore_permissions=True)
				cls._created_items.append(item_key)

		frappe.db.commit()

	@classmethod
	def _insert_test_invoices(cls):
		"""
		Directly INSERT Sales Invoice and Sales Invoice Item rows with docstatus=1.
		This bypasses account/tax validation that would be needed for a real submission.
		"""
		now = now_datetime()
		now_str = str(now)

		# Build customer -> TEST-WAVES-code map
		cust_map = {}
		for inv in cls.sales:
			cust_map[inv["customer_code"]] = TEST_PREFIX + inv["customer_code"]

		for idx, inv in enumerate(cls.sales):
			inv_name = f"{TEST_PREFIX}{inv['invoice_number']}"
			cust_key = cust_map.get(inv["customer_code"], TEST_PREFIX + inv["customer_code"])

			# Parse amounts (remove commas)
			grand_total = float(inv["invoice_amount"].replace(",", ""))
			outstanding = float(inv["outstanding_amount"].replace(",", ""))

			company_name = cls.company or "Test Waves Company"

			# Insert parent invoice
			frappe.db.sql(
				"""
				INSERT IGNORE INTO `tabSales Invoice`
					(name, docstatus, posting_date, customer, customer_name,
					 grand_total, outstanding_amount, po_no, company,
					 creation, modified, owner, modified_by, idx)
				VALUES
					(%s, 1, %s, %s, %s,
					 %s, %s, %s, %s,
					 %s, %s, 'Administrator', 'Administrator', %s)
				""",
				(
					inv_name,
					TEST_DATE,
					cust_key,
					inv["customer_name"].strip()[:140],
					grand_total,
					outstanding,
					inv.get("sales_order_number") or "",
					company_name,
					now_str,
					now_str,
					idx + 1,
				),
			)
			cls._inserted_invoice_names.append(inv_name)

			# Insert line items
			for line_idx, line in enumerate(inv.get("salesline", [])):
				item_key = TEST_PREFIX + line["product_code"]
				qty = float(str(line.get("qty", 1)).replace(",", ""))
				rate = float(str(line.get("unit_rate", 0)).replace(",", ""))
				amount = float(str(line.get("gross_total", 0)).replace(",", ""))
				net_amount = float(str(line.get("tax_base_amount", 0)).replace(",", ""))

				frappe.db.sql(
					"""
					INSERT IGNORE INTO `tabSales Invoice Item`
						(name, parent, parenttype, parentfield, docstatus,
						 item_code, item_name, description, uom, qty, rate,
						 amount, net_amount, discount_percentage,
						 creation, modified, owner, modified_by, idx)
					VALUES
						(%s, %s, 'Sales Invoice', 'items', 1,
						 %s, %s, %s, %s, %s, %s,
						 %s, %s, 0,
						 %s, %s, 'Administrator', 'Administrator', %s)
					""",
					(
						f"{inv_name}-{line_idx + 1}",
						inv_name,
						item_key,
						line.get("item_description", "")[:140],
						line.get("item_description", "")[:140],
						line.get("unit", "NOS"),
						qty,
						rate,
						amount,
						net_amount,
						now_str,
						now_str,
						line_idx + 1,
					),
				)
				cls._inserted_item_parents.append(inv_name)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		"""Clean up all test records."""
		if cls._inserted_invoice_names:
			names_placeholder = ", ".join(["%s"] * len(cls._inserted_invoice_names))
			frappe.db.sql(
				f"DELETE FROM `tabSales Invoice Item` WHERE parent IN ({names_placeholder})",
				cls._inserted_invoice_names,
			)
			frappe.db.sql(
				f"DELETE FROM `tabSales Invoice` WHERE name IN ({names_placeholder})",
				cls._inserted_invoice_names,
			)

		for item_key in cls._created_items:
			try:
				frappe.delete_doc("Item", item_key, ignore_permissions=True, force=True)
			except Exception:
				pass

		for cust_key in cls._created_customers:
			try:
				frappe.delete_doc("Customer", cust_key, ignore_permissions=True, force=True)
			except Exception:
				pass

		for uom_name in cls._created_uoms:
			try:
				frappe.delete_doc("UOM", uom_name, ignore_permissions=True, force=True)
			except Exception:
				pass

		frappe.db.commit()
		super().tearDownClass()

	def test_sample_data_file_exists(self):
		"""Sanity check: the sample data file must be present."""
		self.assertTrue(os.path.exists(SAMPLE_DATA_PATH), f"Sample data not found at {SAMPLE_DATA_PATH}")

	def test_sample_data_has_196_invoices(self):
		"""The sample data file must contain exactly 196 invoices."""
		self.assertEqual(len(self.sales), 196)

	def test_payload_invoice_count(self):
		"""
		_build_waves_payload should return exactly 196 invoices for 2026-06-17.
		We filter by the test invoice names to isolate from other data.
		"""
		from waves_sync.api.export import _build_waves_payload

		payload, total_inv, total_items = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		# Filter only the test invoices we inserted
		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]

		# All 196 test invoices should appear (outstanding_amount > 0 for all sample data)
		self.assertEqual(
			len(test_sales),
			196,
			f"Expected 196 test invoices, got {len(test_sales)}. "
			f"Total payload invoices: {total_inv}",
		)

	def test_payload_line_item_count(self):
		"""Total line items across all 196 test invoices should be 946."""
		from waves_sync.api.export import _build_waves_payload

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]
		total_items = sum(len(s.get("salesline", [])) for s in test_sales)

		self.assertEqual(
			total_items,
			946,
			f"Expected 946 line items, got {total_items}",
		)

	def test_payload_invoice_structure(self):
		"""Each invoice entry must have all required top-level keys."""
		from waves_sync.api.export import _build_waves_payload

		required_keys = {
			"invoice_date",
			"invoice_number",
			"invoice_amount",
			"outstanding_amount",
			"sales_order_number",
			"sales_person_code",
			"sales_person_name",
			"customer_code",
			"customer_name",
			"salesline",
		}

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]
		for sale in test_sales:
			missing = required_keys - set(sale.keys())
			self.assertFalse(missing, f"Invoice {sale['invoice_number']} missing keys: {missing}")

	def test_payload_line_item_structure(self):
		"""Each line item must have all required keys."""
		from waves_sync.api.export import _build_waves_payload

		required_line_keys = {
			"product_code",
			"item_description",
			"unit",
			"qty",
			"unit_rate",
			"tax_base_amount",
			"gross_total",
			"cd",
		}

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]
		for sale in test_sales:
			for line in sale.get("salesline", []):
				missing = required_line_keys - set(line.keys())
				self.assertFalse(
					missing,
					f"Line item in {sale['invoice_number']} missing keys: {missing}",
				)

	def test_invoice_date_format(self):
		"""All test invoice dates should be formatted as DD/MM/YYYY."""
		from waves_sync.api.export import _build_waves_payload

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]
		for sale in test_sales:
			self.assertEqual(
				sale["invoice_date"],
				"17/06/2026",
				f"Invoice {sale['invoice_number']} has wrong date format: {sale['invoice_date']}",
			)

	def test_customer_code_populated(self):
		"""Customer code field should be populated (not empty) for test invoices."""
		from waves_sync.api.export import _build_waves_payload

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		test_sales = [s for s in payload["sales"] if s["invoice_number"].startswith(TEST_PREFIX)]
		for sale in test_sales:
			self.assertTrue(
				sale["customer_code"],
				f"Invoice {sale['invoice_number']} has empty customer_code",
			)

	def test_json_serializable(self):
		"""The payload must be fully JSON serializable."""
		from waves_sync.api.export import _build_waves_payload

		payload, _, _ = _build_waves_payload(
			TEST_DATE,
			TEST_DATE,
			company=None,
			include_zero_outstanding=False,
		)

		# This should not raise
		json_str = json.dumps(payload, ensure_ascii=False)
		self.assertIsInstance(json_str, str)
		self.assertIn('"sales"', json_str)
