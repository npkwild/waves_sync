def set_custom_id(doc, method):
    if doc.custom_id != doc.name:
        doc.db_set("custom_id", doc.name, update_modified=False)