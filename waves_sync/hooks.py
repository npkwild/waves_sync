app_name = "waves_sync"
app_title = "Waves Sync"
app_publisher = "Faircode Infotech"
app_description = "Export ERPNext sales invoice data to Waves application JSON format"
app_email = "nakul@faircodetech.com"
app_license = "mit"

fixtures = [
	{"dt": "Custom Field", "filters": [
        ["name", "in", [
            "Sales Invoice-custom_waves_invoice_number",
            "Customer-custom_customer_code"
        ]]]},
        {
            "dt": "Property Setter",
    "filters":[
        ["name", "in",[
            "Sales Invoice-main-naming_rule",
            "Sales Invoice-main-autoname",
            "Sales Invoice-main-field_order"

        ]]
    ]
        },
]

doctype_js = {
	"Waves Sync Log": "public/js/waves_sync_log.js",
}

app_include_css = "/assets/waves_sync/css/waves_sync.css"
