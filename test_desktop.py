from __future__ import annotations

import desktop_app

app = desktop_app.ContactDirectoryDesktop()
app.update_idletasks()
app.update()
assert app.title() == 'Contact Directory'
assert app.notebook.tabs()
assert app.directory_tree is not None
assert set(['source', 'industry', 'source_type', 'source_category', 'category']).issubset(app.catalog_controls)
assert app.filter_phone_combo is not None
assert app.import_scroll_canvas is not None
app.update_idletasks()
scrollregion = app.import_scroll_canvas.cget('scrollregion')
assert scrollregion
app.destroy()
print({'status': 'ok', 'desktop_gui': 'started_and_closed', 'tabs': 4})
