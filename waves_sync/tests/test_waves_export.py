import unittest
import frappe
from frappe.tests import UnitTestCase
from unittest.mock import patch, MagicMock
from datetime import date
from waves_sync.api.export import (
	_fmt_amount,
	_fmt_date,
	_fmt_qty,
	_fmt_rate,
	_fmt_cd,
	_build_waves_payload,
)


class TestFormatHelpers(UnitTestCase):
	def test_fmt_amount_integer(self):
		self.assertEqual(_fmt_amount(44457), "44,457")

	def test_fmt_amount_zero(self):
		self.assertEqual(_fmt_amount(0), "0")

	def test_fmt_amount_large(self):
		self.assertEqual(_fmt_amount(1234567), "1,234,567")

	def test_fmt_date_date_object(self):
		self.assertEqual(_fmt_date(date(2026, 6, 17)), "17/06/2026")

	def test_fmt_date_none(self):
		self.assertEqual(_fmt_date(None), "")

	def test_fmt_qty_integer(self):
		self.assertEqual(_fmt_qty(10.0), "10")

	def test_fmt_qty_decimal(self):
		self.assertEqual(_fmt_qty(2.5), "2.5")

	def test_fmt_rate(self):
		self.assertEqual(_fmt_rate(116.31), "116.31")

	def test_fmt_cd_zero(self):
		self.assertEqual(_fmt_cd(0), "0")

	def test_fmt_cd_ten(self):
		self.assertEqual(_fmt_cd(10), "10")

	def test_fmt_cd_float(self):
		self.assertEqual(_fmt_cd(3.0), "3")


class TestBuildWavesPayload(UnitTestCase):
	"""Integration-style tests that mock frappe.get_all."""

	def _make_invoice(self):
		return {
			"name": "INV-001",
			"posting_date": date(2026, 1, 15),
			"customer": "CUST-001",
			"customer_name": "Test Customer Ltd",
			"grand_total": 10000,
			"outstanding_amount": 10000,
			"po_no": "PO-123",
		}

	def _make_item(self):
		return {
			"parent": "INV-001",
			"item_code": "ITEM-001",
			"item_name": "Test Item",
			"description": "Test Item Desc",
			"uom": "NOS",
			"qty": 5,
			"rate": 2000.0,
			"net_amount": 8474.0,
			"amount": 10000.0,
			"discount_percentage": 0,
		}

	@patch("waves_sync.api.export.frappe.get_all")
	def test_empty_result_when_no_invoices(self, mock_get_all):
		mock_get_all.return_value = []
		payload, total_inv, total_items = _build_waves_payload("2026-01-01", "2026-01-31")
		self.assertEqual(payload, {"sales": []})
		self.assertEqual(total_inv, 0)
		self.assertEqual(total_items, 0)

	@patch("waves_sync.api.export.frappe.get_all")
	def test_single_invoice_structure(self, mock_get_all):
		invoice = self._make_invoice()
		item = self._make_item()

		def side_effect(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				return [invoice]
			if doctype == "Sales Team":
				return []
			if doctype == "Sales Person":
				return []
			if doctype == "Sales Invoice Item":
				filters = kwargs.get("filters", {})
				# second call for SO is distinguished by filter key
				if "sales_order" in str(filters):
					return []
				return [item]
			if doctype == "Customer":
				return [{"name": "CUST-001", "customer_name": "Test Customer Ltd", "custom_customer_code": "CU001"}]
			return []

		mock_get_all.side_effect = side_effect
		payload, total_inv, total_items = _build_waves_payload("2026-01-01", "2026-01-31")

		self.assertEqual(total_inv, 1)
		self.assertEqual(total_items, 1)
		sale = payload["sales"][0]
		self.assertEqual(sale["invoice_number"], "INV-001")
		self.assertEqual(sale["invoice_date"], "15/01/2026")
		self.assertEqual(sale["customer_code"], "CU001")
		self.assertEqual(sale["customer_name"], "Test Customer Ltd")
		self.assertEqual(len(sale["salesline"]), 1)
		line = sale["salesline"][0]
		self.assertEqual(line["product_code"], "ITEM-001")
		self.assertEqual(line["unit"], "NOS")
		self.assertEqual(line["qty"], "5")
		self.assertEqual(line["unit_rate"], "2000.00")

	@patch("waves_sync.api.export.frappe.get_all")
	def test_sales_person_lookup(self, mock_get_all):
		invoice = self._make_invoice()

		def side_effect(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				return [invoice]
			if doctype == "Sales Team":
				return [{"parent": "INV-001", "sales_person": "SP-001"}]
			if doctype == "Sales Person":
				return [{"name": "SP-001", "sales_person_name": "John Doe", "custom_sales_person_code": "2210"}]
			if doctype == "Sales Invoice Item":
				filters = kwargs.get("filters", {})
				if "sales_order" in str(filters):
					return []
				return [self._make_item()]
			if doctype == "Customer":
				return [{"name": "CUST-001", "customer_name": "Test Customer Ltd", "custom_customer_code": "CU001"}]
			return []

		mock_get_all.side_effect = side_effect
		payload, _, _ = _build_waves_payload("2026-01-01", "2026-01-31")
		sale = payload["sales"][0]
		self.assertEqual(sale["sales_person_code"], "2210")
		self.assertEqual(sale["sales_person_name"], "John Doe")

	@patch("waves_sync.api.export.frappe.get_all")
	def test_sales_order_number_from_item(self, mock_get_all):
		invoice = self._make_invoice()

		def side_effect(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				return [invoice]
			if doctype == "Sales Team":
				return []
			if doctype == "Sales Person":
				return []
			if doctype == "Sales Invoice Item":
				filters = kwargs.get("filters", {})
				if "sales_order" in str(filters):
					return [{"parent": "INV-001", "sales_order": "SO-001"}]
				return [self._make_item()]
			if doctype == "Customer":
				return [{"name": "CUST-001", "customer_name": "Test Customer Ltd", "custom_customer_code": "CU001"}]
			return []

		mock_get_all.side_effect = side_effect
		payload, _, _ = _build_waves_payload("2026-01-01", "2026-01-31")
		sale = payload["sales"][0]
		self.assertEqual(sale["sales_order_number"], "SO-001")

	@patch("waves_sync.api.export.frappe.get_all")
	def test_falls_back_to_po_no_when_no_so(self, mock_get_all):
		invoice = self._make_invoice()

		def side_effect(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				return [invoice]
			if doctype == "Sales Team":
				return []
			if doctype == "Sales Person":
				return []
			if doctype == "Sales Invoice Item":
				filters = kwargs.get("filters", {})
				if "sales_order" in str(filters):
					return []
				return [self._make_item()]
			if doctype == "Customer":
				return [{"name": "CUST-001", "customer_name": "Test Customer Ltd", "custom_customer_code": "CU001"}]
			return []

		mock_get_all.side_effect = side_effect
		payload, _, _ = _build_waves_payload("2026-01-01", "2026-01-31")
		sale = payload["sales"][0]
		self.assertEqual(sale["sales_order_number"], "PO-123")

	@patch("waves_sync.api.export.frappe.get_all")
	def test_company_filter_applied(self, mock_get_all):
		mock_get_all.return_value = []
		_build_waves_payload("2026-01-01", "2026-01-31", company="Test Co")
		first_call_args = mock_get_all.call_args_list[0]
		filters_used = first_call_args[1].get("filters") or first_call_args[0][1]
		self.assertIn("company", filters_used)
		self.assertEqual(filters_used["company"], "Test Co")

	@patch("waves_sync.api.export.frappe.get_all")
	def test_include_zero_outstanding_filter(self, mock_get_all):
		mock_get_all.return_value = []
		_build_waves_payload("2026-01-01", "2026-01-31", include_zero_outstanding=True)
		first_call_args = mock_get_all.call_args_list[0]
		filters_used = first_call_args[1].get("filters") or first_call_args[0][1]
		self.assertNotIn("outstanding_amount", filters_used)

	@patch("waves_sync.api.export.frappe.get_all")
	def test_exclude_zero_outstanding_by_default(self, mock_get_all):
		mock_get_all.return_value = []
		_build_waves_payload("2026-01-01", "2026-01-31", include_zero_outstanding=False)
		first_call_args = mock_get_all.call_args_list[0]
		filters_used = first_call_args[1].get("filters") or first_call_args[0][1]
		self.assertIn("outstanding_amount", filters_used)
