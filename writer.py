"""
writer.py — write the three Excel outputs using openpyxl (no pandas).

Files:
  search_results.xlsx        (overwritten each search run)
  closed_source_papers.xlsx  (appended as papers are found closed)
  extracted_data.xlsx        (appended as papers are extracted)

All appends are header-driven: values are placed according to the sheet's
actual header, never by position. When a caller asks for columns the sheet does
not have yet, they are appended as new headers and existing rows are left
untouched, so older workbooks keep working after the output layout grows.
"""

from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return value


def _header_text(value):
    return "" if value is None else str(value).strip()


def _header_cells(ws):
    """Return [(column_index, header_name), ...] for non-empty cells in row 1."""
    if ws.max_row < 1:
        return []
    cells = []
    for cell in ws[1]:
        name = _header_text(cell.value)
        if name:
            cells.append((cell.column, name))
    return cells


def _header_names(ws):
    return [name for _col, name in _header_cells(ws)]


def _column_index(ws, name):
    for col, header in _header_cells(ws):
        if header == name:
            return col
    return None


def _sheet_has_data(ws):
    return any(
        cell.value not in (None, "")
        for row in ws.iter_rows()
        for cell in row
    )


def _last_data_row(ws):
    """Last row (1-based) that has at least one non-empty cell.

    `ws.max_row` can be inflated by formatting/blank cells; using the real data
    extent prevents filling defaults into thousands of empty rows.
    """
    last = 1
    for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if any(cell.value not in (None, "") for cell in row):
            last = row_idx
    return last


def _style_header(ws):
    for cell in ws[1]:
        cell.font = Font(bold=True)


def _hyperlink_doi(ws):
    """Make the sheet's 'doi' column clickable (https://doi.org/<doi>)."""
    col = _column_index(ws, "doi")
    if col is None:
        return
    for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
        cell = row[0]
        doi = str(cell.value or "").strip()
        if doi:
            url = doi if doi.startswith(("http://", "https://")) else f"https://doi.org/{doi}"
            cell.hyperlink = url
            cell.font = Font(color="0563C1", underline="single")


def _write_header(ws, columns):
    """Write a header row without losing existing data."""
    if _sheet_has_data(ws):
        ws.insert_rows(1)
    for col, name in enumerate(columns, start=1):
        ws.cell(row=1, column=col, value=str(name))


def _append_columns(ws, columns, defaults=None):
    """Append missing `columns` to the sheet header.

    Existing headers keep their position and values. New headers are added in
    the order requested. If `defaults` contains one of the new names, every
    existing data row gets that value where the new cell is still blank.

    Returns `(header, added)` where `header` is the actual header after the
    operation and `added` is the list of column names that were appended.
    """
    defaults = defaults or {}
    header = _header_names(ws)
    if not header:
        header = [str(c) for c in columns]
        _write_header(ws, header)
        return header, header

    existing = set(header)
    added = []
    for name in columns:
        name = str(name)
        if name not in existing:
            added.append(name)
            existing.add(name)

    if not added:
        return header, []

    # Append after every used column so an internal/trailing blank header cell
    # (or stray data without a header) can never be overwritten.
    start_col = max(ws.max_column, 1) + 1
    for offset, name in enumerate(added):
        ws.cell(row=1, column=start_col + offset, value=name)
    header.extend(added)

    last = _last_data_row(ws)
    for name in added:
        if name not in defaults:
            continue
        col = _column_index(ws, name)
        default = defaults[name]
        for row_idx in range(2, last + 1):
            cell = ws.cell(row=row_idx, column=col)
            if cell.value in (None, ""):
                cell.value = _cell(default)

    return header, added


def _load_or_create(path, columns):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        wb = load_workbook(path)
        ws = wb.active
        return wb, ws
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append([str(c) for c in columns])
    return wb, ws


def ensure_columns(path, columns, defaults=None):
    """Add any missing `columns` to an existing workbook, preserving all data.

    - Existing headers and cell values are never changed or reordered.
    - Missing headers are appended in the order given by `columns`.
    - `defaults` (optional) maps new column names to a value that is filled
      into existing rows whose new cell is blank.
    - Header styling is refreshed before saving.

    Returns True when an existing file was opened and saved, False when the
    file does not exist (nothing is created).
    """
    path = Path(path)
    if not path.exists():
        return False
    wb = load_workbook(path)
    ws = wb.active
    _header, added = _append_columns(ws, columns, defaults)
    if not added:
        wb.close()
        return True
    _style_header(ws)
    _hyperlink_doi(ws)
    wb.save(path)
    return True


def backfill_paper_keys(path, key_column="paper_key", doi_column="doi"):
    """Fill blank `paper_key` cells from an existing DOI column.

    Only DOI-based identities are derived (``doi:<lowercased DOI>``). Rows
    without a DOI are left blank on purpose: an identity is never invented.

    Returns the number of rows filled (0 when the file/columns do not exist).
    """
    path = Path(path)
    if not path.exists():
        return 0
    wb = load_workbook(path)
    ws = wb.active
    key_col = _column_index(ws, key_column)
    doi_col = _column_index(ws, doi_column)
    if key_col is None or doi_col is None:
        return 0

    filled = 0
    for row_idx in range(2, _last_data_row(ws) + 1):
        key_cell = ws.cell(row=row_idx, column=key_col)
        if key_cell.value not in (None, ""):
            continue
        doi = _header_text(ws.cell(row=row_idx, column=doi_col).value)
        if not doi:
            continue
        key_cell.value = f"doi:{doi.lower()}"
        filled += 1
    if filled:
        wb.save(path)
    return filled


def write_xlsx(path, columns, rows):
    """Overwrite `path` with a fresh sheet: header + rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    header = [str(c) for c in columns]
    ws.append(header)
    for row in rows:
        ws.append([_cell(row.get(c, "")) for c in header])
    _style_header(ws)
    _hyperlink_doi(ws)
    wb.save(path)


def append_xlsx(path, columns, rows):
    """Append rows to `path` (create it with a header first if missing).

    Values are written by header name, not by position. If the sheet already
    exists with extra or reordered columns, each row is mapped onto the sheet's
    real header; missing requested columns are appended first. This keeps old
    data readable when the output layout gains new columns.
    """
    path = Path(path)
    wb, ws = _load_or_create(path, columns)
    _append_columns(ws, columns)
    _style_header(ws)

    # Build each row against the sheet's real header positions. This also
    # handles a header with an internal blank cell without shifting values.
    header_cells = _header_cells(ws)
    width = max(ws.max_column, 1)
    for row in rows:
        values = [""] * width
        for col, name in header_cells:
            values[col - 1] = _cell(row.get(name, ""))
        ws.append(values)

    _hyperlink_doi(ws)
    wb.save(path)
