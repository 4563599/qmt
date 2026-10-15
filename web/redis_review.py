"""Render the current daily review layout using Redis records only."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re


LAYOUT_PATH = Path(__file__).with_name('review_layout.json')


class _StyleRecorder:
    """Reuse the daily writer's pure styles/rich text without network calls."""

    def __init__(self, rows):
        self.rows = rows
        self.groups = []

    def _json_request(self, method, path, *, token=None, body=None):
        if path.endswith('/styles_batch_update'):
            self.groups.extend(body['data'])
        elif path.endswith('/values_batch_update'):
            for change in body['valueRanges']:
                address = change['range'].split('!', 1)[-1].split(':', 1)[0]
                match = re.fullmatch(r'([A-Z]+)(\d+)', address)
                column = 0
                for char in match[1]:
                    column = column * 26 + ord(char) - 64
                self.rows[int(match[2]) - 1][column - 1] = change['values'][0][0]
        return {}


def _fixed_styles(module, values, start):
    groups = []
    heights = [31] * len(values)
    merges = []

    def add(offset, start_col='A', end_col='H', *, size=11, color='#2E2A35',
            fill='#FFFFFF', bold=False, height=None):
        row = start + offset
        groups.append({'ranges': [f'{start_col}{row}:{end_col}{row}'], 'style': {
            'font': {'fontFamily': 'Microsoft YaHei', 'fontSize': f'{size}pt/1.5', 'bold': bold},
            'hAlign': 0, 'vAlign': 1, 'foreColor': color, 'backColor': fill,
        }})
        if height is not None:
            heights[offset] = height

    for offset, row in enumerate(values):
        text = module.hyperlink_display_text(row[0]).strip()
        if text in ('同花顺板块', '同花顺板块相关个股', 'Final'):
            add(offset, size=16, color='#51336C', bold=True, height=39)
        elif text in ('板块名称', '全阳'):
            add(offset, size=12, color='#53475F', fill='#E4D5EE', bold=True, height=33)
        elif text.startswith('20天阴线='):
            add(offset, color='#53475F', fill='#F5F0F8', bold=True)
        elif text in ('1全阳', '3.5拉升') or text.startswith(('条件1 ZDZF8', '条件2 4不碰')):
            add(offset, 'A', 'A', size=14)
        elif text in (module.YIN4_SECTION_TITLE, module.YIN_BELOW_SECTION_TITLE):
            add(offset, 'A', 'A', size=14, height=39)
        elif text in module.YIN4_NOTES:
            add(offset, 'A', 'A', color='#D93025', bold=True)
        elif text == module.YIN_MA5_CRITERIA:
            add(offset, color='#5D3A78', fill='#F5F0F8', bold=True)
        elif text == module.YIN_BELOW_GUIDE:
            add(offset, 'A', 'A', color='#9333EA', bold=True)
        elif text == module.FIVE_PRIORITY_FIELDS[0][1]:
            add(offset, 'A', 'B', color='#51336C', fill='#F5F0F8', bold=True)
        elif text.startswith('4. 全阳阴线后面强势K') or text == '5. 涨停复盘':
            add(offset, 'A', 'A', size=14, bold=True, height=100)
            add(offset, 'B', 'D', color='#51336C', bold=True)
        elif text.startswith('优先选出阴后强'):
            add(offset, size=12, color='#51336C', fill='#F5F0F8', bold=True, height=70)
        elif text in ('光脚阳线或者光头阳线', '十字星（上下阴线很少，中体产业吃亏）', '拉升6.5阴被吃'):
            add(offset, 'A', 'A', color='#9333EA', bold=True)
        elif text.startswith('阴小4（必做任务）和阴线跌破5日线'):
            add(offset, 'A', 'C', size=12, color='#E33D3D', bold=True, height=70)
            merges.append({'row': start + offset, 'column': 1, 'row_span': 1, 'column_span': 3})
        elif text.startswith('筛选出K线最漂亮'):
            add(offset, 'A', 'A', size=12, color='#E33D3D', bold=True, height=39)
        elif text.startswith(('优先级', '低位优先级')):
            add(offset, 'A', 'A', color='#9333EA', fill='#FFF152', bold=True, height=49 if text.startswith('优先级1') else 31)
            if text.startswith('优先级5.'):
                add(offset, 'B', 'H', size=10, color='#000000')
            else:
                add(offset, 'B', 'H', color='#9333EA', bold=True)
        elif any(module.hyperlink_display_text(value).strip() == '无' for value in row):
            for column, value in enumerate(row):
                if module.hyperlink_display_text(value).strip() == '无':
                    add(offset, chr(65 + column), chr(65 + column), color='#222222', bold=True)
        if text == '5. 涨停复盘':
            add(offset, 'A', 'A', size=14)
        if text in ('1全阳', module.YIN4_SECTION_TITLE):
            merges.append({'row': start + offset, 'column': 2, 'row_span': 1, 'column_span': 7})
    return groups, heights, merges


def build_review(module, run_date: date, ths, pz1, qy2, related, cls_values=None):
    """Insert fresh stock regions into the frozen fixed-text layout."""
    template = json.loads(LAYOUT_PATH.read_text(encoding='utf-8'))
    generated, source_layout = module.build_sheet_content(
        ths, pz1, qy2_stocks=qy2, related_pz_stocks=related,
    )
    # Keep the published web app usable when only the lightweight fallback
    # builder is available.  The full builder supplies region metadata and
    # is used in production; the fallback returns a complete row list but
    # intentionally has no Feishu-specific region/style helpers.
    if not source_layout.get('regions'):
        stock_codes = {}
        for row_index, row in enumerate(generated, start=1):
            for column_index, value in enumerate(row, start=1):
                text_value = module.hyperlink_display_text(value)
                code = re.search(r'(?<!\d)\d{6}(?!\d)', text_value)
                if code:
                    stock_codes[f'{chr(64 + column_index)}{row_index}'] = code[0]
        return {
            'values': generated,
            'layout': {'stock_codes': stock_codes, 'regions': []},
            'style_groups': [],
            'column_widths_px': [456, 392, 456, 392, 456, 80, 456, 80],
            'row_heights_px': [31] * len(generated),
            'merged_ranges': [],
        }
    regions = {region['title']: region for region in source_layout['regions']}
    names = {
        'boards': '同花顺板块', 'related': '同花顺板块相关个股',
        'condition1': '条件1 ZDZF8(拉伸6.5)', 'condition2': '条件2 4不碰',
        'five_priority': '3.5拉升',
    }
    rows, heights, merges = [], [], []
    layout = {'stock_codes': {}, 'regions': []}
    recorder = _StyleRecorder(rows)

    def date_links(value):
        if not isinstance(value, list):
            return value
        for segment in value:
            if isinstance(segment, dict) and segment.get('type') == 'url':
                segment['link'] = re.sub(r'date=\d{4}-\d{2}-\d{2}',
                                         'date=' + run_date.isoformat(), segment.get('link', ''))
        return value

    for block in template['blocks']:
        start = len(rows) + 1
        if 'fixed' in block:
            fixed = [[date_links(value) for value in row] for row in deepcopy(block['fixed'])]
            groups, fixed_heights, fixed_merges = _fixed_styles(module, fixed, start)
            rows.extend(fixed)
            heights.extend(fixed_heights)
            recorder.groups.extend(groups)
            merges.extend(fixed_merges)
            continue

        kind = block['data']
        source_start, source_end = 0, -1
        group_offsets, stock_offsets = [], []
        if kind == 'qy2':
            data, group_offsets, stock_offsets = module.build_qy2_group_rows(qy2)
        elif kind == 'yin_ma5':
            data, group_offsets = module.build_yin_ma5_group_rows(pz1)
        else:
            region = regions[names[kind]]
            source_start, source_end = region['data_start_row'], region['data_end_row']
            data = deepcopy(generated[source_start - 1:source_end])
        rows.extend(data)
        # These are the same styling/rich text functions as daily Feishu.
        module._style_template_data(
            recorder, '', '', '', start, data,
            'condition' if kind.startswith('condition') else kind,
            source_start=source_start, source_end=source_end, source_layout=source_layout,
            boards=module.prepare_ths_boards(ths) if kind == 'boards' else None,
            qy2_groups=group_offsets, qy2_stocks=stock_offsets,
        )
        module._restore_stock_gain_segments(recorder, '', '', '', start, data)
        heights.extend([31] * len(data))
        for address, code in source_layout.get('stock_codes', {}).items():
            match = re.fullmatch(r'([A-Z]+)(\d+)', address)
            if match and source_start <= int(match[2]) <= source_end:
                layout['stock_codes'][f'{match[1]}{start + int(match[2]) - source_start}'] = code
        if kind in ('qy2', 'yin_ma5'):
            for offset, row in enumerate(data):
                text = module.hyperlink_display_text(row[0])
                code = re.search(r'(?<!\d)\d{6}(?!\d)', text)
                if code:
                    layout['stock_codes'][f'A{start + offset}'] = code[0]
        layout['regions'].append({'kind': kind, 'start_row': start, 'rows': len(data)})
        rows.extend([[''] * 8 for _ in range(2)])
        heights.extend([31, 31])

    if cls_values:
        target = f'{module.FOCUS_PAGE_URL}?date={run_date.isoformat()}&page=8'
        for address, linked, is_link in module.prepare_cls_stock_links(rows, cls_values, target):
            if is_link:
                match = re.fullmatch(r'([A-Z])(\d+)', address)
                rows[int(match[2]) - 1][ord(match[1]) - 65] = linked
    return {
        'values': rows, 'layout': layout, 'style_groups': recorder.groups,
        'column_widths_px': template['column_widths_px'], 'row_heights_px': heights,
        'merged_ranges': merges,
    }
