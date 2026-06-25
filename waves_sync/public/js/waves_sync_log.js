const STATUS_COLOR = { Success: "green", Error: "red", Pending: "orange" };

frappe.ui.form.on("Waves Sync Log", {
	refresh(frm) {
		frm.trigger("render_sync_button");
		// Defer coloring so Frappe has finished rendering the grid rows
		frappe.after_ajax(() => frm.trigger("color_invoice_rows"));
	},

	file_attachment(frm) {
		frm.trigger("render_sync_button");
	},

	render_sync_button(frm) {
		frm.remove_custom_button(__("Sync Invoices"));
		if (frm.is_new() || !frm.doc.file_attachment) return;

		const btn = frm.add_custom_button(__("Sync Invoices"), () => {
			frappe.call({
				method: "waves_sync.api.sync.process_log_file",
				args: { log_name: frm.doc.name },
				freeze: true,
				freeze_message: __("Reading file and processing invoices…"),
				callback(r) {
					if (!r.exc && r.message) {
						const { summary, status } = r.message;
						frappe.show_alert(
							{ message: summary, indicator: status === "Success" ? "green" : "red" },
							8
						);
						frm.reload_doc();
					}
				},
			});
		});
		btn.addClass("btn-primary");
	},

	color_invoice_rows(frm) {
		if (!frm.fields_dict.invoice_rows) return;
		const grid = frm.fields_dict.invoice_rows.grid;
		(grid.grid_rows || []).forEach(_apply_row_color);
	},
});

// Re-apply color whenever status changes in a row
frappe.ui.form.on("Waves Sync Invoice Row", {
	status(frm, cdt, cdn) {
		const row = frappe.get_doc(cdt, cdn);
		const grid_row = frm.fields_dict.invoice_rows.grid.get_row(cdn);
		if (grid_row) _apply_row_color(grid_row);
	},
});

function _apply_row_color(grid_row) {
	if (!grid_row || !grid_row.doc || !grid_row.row) return;
	const status = grid_row.doc.status;
	const color = STATUS_COLOR[status];

	grid_row.row.removeClass("ws-row-success ws-row-error ws-row-pending");
	if (color) grid_row.row.addClass(`ws-row-${status.toLowerCase()}`);

	// Bold + color the status cell text
	const cell = grid_row.row.find('[data-fieldname="status"] .static-area, [data-fieldname="status"] .like-disabled-input');
	if (cell.length && color) {
		const hex = { green: "#2f9e44", red: "#c92a2a", orange: "#e67700" }[color] || "";
		cell.css({ color: hex, "font-weight": "600" });
	}
}
