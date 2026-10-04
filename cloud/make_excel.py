"""Monthly Excel downloads for the live page: <page>/excel/YYYY-MM.xlsx, plus excel/index.json (list of months).

Each workbook has two sheets, built from the complete days archived in data/:
  Resumen diario   one row per day (solar, on-site, bought from grid, injected, total use, solar coverage) + month total
  Lecturas 15 min  every 15-minute reading of the month

Same definitions as the page:
  solar = AGRIM02P col 1, injected = DAGSR01P col 1, grid = DAGSR01P col 2,
  on-site = max(0, solar - injected) per interval, total use = on-site + grid.

Plain standard library (no openpyxl), and the files are byte-for-byte reproducible: a workbook is
only rewritten when its data changes, so the repository does not churn.
"""
import datetime as dt
import io
import json
import os
import zipfile
from xml.sax.saxutils import escape

MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
         'septiembre', 'octubre', 'noviembre', 'diciembre']
EPOCH = dt.date(1899, 12, 30)

# cell styles (index into cellXfs below)
S_TXT, S_HEAD, S_DATE, S_KWH3, S_KWH1, S_PCT, S_TOTTXT, S_TOTKWH, S_TOTPCT, S_TITLE, S_NOTE = range(11)

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/><numFmt numFmtId="165" formatCode="#,##0.000"/><numFmt numFmtId="166" formatCode="#,##0.0"/></numFmts>
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="14"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF2F1EC"/></patternFill></fill></fills>
<borders count="3"><border/><border><bottom style="medium"/></border><border><top style="thin"/></border></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="11">
<xf xfId="0"/>
<xf xfId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1"><alignment wrapText="1" vertical="center"/></xf>
<xf xfId="0" numFmtId="164" applyNumberFormat="1"/>
<xf xfId="0" numFmtId="165" applyNumberFormat="1"/>
<xf xfId="0" numFmtId="166" applyNumberFormat="1"/>
<xf xfId="0" numFmtId="9" applyNumberFormat="1"/>
<xf xfId="0" fontId="1" borderId="2" applyFont="1" applyBorder="1"/>
<xf xfId="0" numFmtId="166" fontId="1" borderId="2" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
<xf xfId="0" numFmtId="9" fontId="1" borderId="2" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
<xf xfId="0" fontId="2" applyFont="1"/>
<xf xfId="0"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def col(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(ref, v, style):
    if v is None:
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(v, tuple):                                   # ('=FORMULA', cached value)
        f, val = v
        cached = '' if val is None else f'<v>{val!r}</v>'
        return f'<c r="{ref}" s="{style}"><f>{escape(f[1:])}</f>{cached}</c>'
    if isinstance(v, str):
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
    return f'<c r="{ref}" s="{style}"><v>{v!r}</v></c>'


def sheet_xml(rows, widths, freeze_row, autofilter=None):
    """rows: list of (row number, [(value, style), ...]) with 1-based row numbers."""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0">'
           f'<pane ySplit="{freeze_row}" topLeftCell="A{freeze_row + 1}" activePane="bottomLeft" state="frozen"/>'
           '</sheetView></sheetViews>',
           '<cols>' + ''.join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + '</cols>',
           '<sheetData>']
    for r, cells in rows:
        ht = ' ht="32" customHeight="1"' if r == freeze_row else ''
        out.append(f'<row r="{r}"{ht}>' + ''.join(cell(f'{col(c)}{r}', v, s) for c, (v, s) in enumerate(cells)) + '</row>')
    out.append('</sheetData>')
    if autofilter:
        out.append(f'<autoFilter ref="{autofilter}"/>')
    out.append('</worksheet>')
    return '\n'.join(out)


def workbook_bytes(sheets):
    """sheets: list of (name, sheet xml). Fixed timestamps, so equal input gives equal bytes."""
    names = [n for n, _ in sheets]
    files = {
        '[Content_Types].xml':
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + ''.join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(len(sheets))) + '</Types>',
        '_rels/.rels':
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        'xl/workbook.xml':
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>' + ''.join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)) + '</sheets>'
            + '<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>',
        'xl/_rels/workbook.xml.rels':
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ''.join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                      for i in range(len(sheets)))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        'xl/styles.xml': STYLES,
    }
    for i, (_, xml) in enumerate(sheets):
        files[f'xl/worksheets/sheet{i + 1}.xml'] = xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode('utf-8'))
    return buf.getvalue()



def month_workbook(ym, days):
    """ym 'YYYY-MM'; days: list of (date, sol[96], inj[96], grd[96]) sorted by date."""
    y, m = map(int, ym.split('-'))
    title = f'Planta Agritur · {MESES[m - 1]} {y}'
    r3, r1 = (lambda x: round(x, 3)), (lambda x: round(x, 1))

    # --- Resumen diario ---
    hdr = ['Fecha', 'Producción solar (kWh)', 'Solar consumida en sitio (kWh)', 'Comprada a la red (kWh)',
           'Inyectada a la red (kWh)', 'Consumo total del sitio (kWh)', 'Cobertura solar (%)']
    rows = [(1, [(title, S_TITLE)]),
            (2, [('Días completos registrados por los medidores SMEC (AGRIM02P solar, DAGSR01P conexión a la red).', S_NOTE)]),
            (4, [(h, S_HEAD) for h in hdr])]
    r = 5
    tot = [0.0] * 5
    for d, sol, inj, grd in days:
        on = [max(0.0, s - j) for s, j in zip(sol, inj)]
        v = [sum(sol), sum(on), sum(grd), sum(inj)]
        v.append(v[1] + v[2])
        for k in range(5):
            tot[k] += v[k]
        cover = ('=IF(F{0}>0,C{0}/F{0},"")'.format(r), round(v[1] / v[4], 4) if v[4] else None)
        rows.append((r, [((d - EPOCH).days, S_DATE)] + [(r1(x), S_KWH1) for x in v] + [(cover, S_PCT)]))
        r += 1
    first, last = 5, r - 1
    totrow = [('Total del mes', S_TOTTXT)]
    for k, c in enumerate('BCDEF'):
        totrow.append(((f'=SUM({c}{first}:{c}{last})', r1(tot[k])), S_TOTKWH))
    totrow.append(((f'=IF(F{r}>0,C{r}/F{r},"")', round(tot[1] / tot[4], 4) if tot[4] else None), S_TOTPCT))
    rows.append((r, totrow))
    rows.append((r + 2, [('Solar consumida en sitio = producción solar − inyectada a la red (por intervalo de 15 minutos). '
                          'Consumo total = solar consumida en sitio + comprada a la red.', S_NOTE)]))
    rows.append((r + 3, [('El detalle de cada 15 minutos está en la hoja «Lecturas 15 min».', S_NOTE)]))
    resumen = sheet_xml(rows, [13, 14, 16, 14, 14, 16, 12], 4)

    # --- Lecturas 15 min ---
    hdr2 = ['Fecha', 'Intervalo', 'Producción solar (kWh)', 'Solar consumida en sitio (kWh)', 'Comprada a la red (kWh)',
            'Inyectada a la red (kWh)', 'Consumo total del sitio (kWh)']
    rows2 = [(1, [(h, S_HEAD) for h in hdr2])]
    r = 2
    hm = lambda k: f'{k * 15 // 60:02d}:{k * 15 % 60:02d}'
    for d, sol, inj, grd in days:
        for i in range(96):
            on = max(0.0, sol[i] - inj[i])
            rows2.append((r, [((d - EPOCH).days, S_DATE), (f'{hm(i)}–{hm(i + 1)}', S_TXT),
                              (r3(sol[i]), S_KWH3), (r3(on), S_KWH3), (r3(grd[i]), S_KWH3),
                              (r3(inj[i]), S_KWH3), (r3(on + grd[i]), S_KWH3)]))
            r += 1
    lecturas = sheet_xml(rows2, [12, 13, 14, 16, 14, 14, 16], 1, f'A1:G{max(r - 1, 1)}')
    return workbook_bytes([('Resumen diario', resumen), ('Lecturas 15 min', lecturas)])


def write_if_changed(path, data):
    try:
        with open(path, 'rb') as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def build_month_workbooks(days, parse, dest):
    """days: {YYYYMMDD: {meter: path}} of complete days (from make_live_page.full_days)."""
    by_month = {}
    for day in sorted(days):
        ag, ds = parse(days[day]['AGRIM02P']), parse(days[day]['DAGSR01P'])
        if min(len(ag), len(ds)) < 96:
            continue
        d = dt.datetime.strptime(day, '%Y%m%d').date()
        by_month.setdefault(f'{d:%Y-%m}', []).append(
            (d, [r[0] for r in ag[:96]], [r[0] for r in ds[:96]], [r[1] for r in ds[:96]]))
    out_dir = os.path.join(dest, 'excel')
    os.makedirs(out_dir, exist_ok=True)
    written = 0
    for ym, rows in sorted(by_month.items()):
        if write_if_changed(os.path.join(out_dir, ym + '.xlsx'), month_workbook(ym, rows)):
            written += 1
    months = {ym: {'dias': len(v), 'hasta': v[-1][0].isoformat()} for ym, v in sorted(by_month.items())}
    write_if_changed(os.path.join(out_dir, 'index.json'),
                     json.dumps({'meses': months}, separators=(',', ':')).encode('utf-8'))
    print(f'excel: {len(months)} month(s), {written} workbook(s) written -> {out_dir}')
