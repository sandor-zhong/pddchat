"""Three CLI entry points share this offline test runner.

No business-table writes, browser launches, or appeal submission functions.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
KINDS = {'negative': '负向申诉', 'reconsideration': '复议', 'aftersale': '售后申诉'}
LIMITS = {'negative': 120, 'reconsideration': 200, 'aftersale': 300}
SOURCE_FILES = {
    'negative': ('appeal_classifier.py', 'appeal_style.py'),
    'reconsideration': ('appeal_classifier.py', 'appeal_wording.py', 'appeal_style.py'),
    'aftersale': ('appeal_classifier.py', 'appeal_wording.py', 'appeal_style.py', 'aftersale_appeal.py'),
}
ORDER_ID = re.compile(r'\d{6}-\d{15}')


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def save_run(path: Path, data: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _messages(value) -> list[dict] | None:
    try:
        parsed = json.loads(value or '[]')
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    if any(not isinstance(m, dict) or not all(k in m for k in ('sender', 'content', 'is_system_message', 'image_urls')) for m in parsed):
        return None
    if any(not isinstance(m['sender'], str) or not isinstance(m['content'], str) or not isinstance(m['image_urls'], list) for m in parsed):
        return None
    return parsed


def _buyer(messages: list[dict]) -> bool:
    return any(not m['is_system_message'] and '*' in m['sender'] and (m['content'].strip() or m['image_urls']) for m in messages)


def select_records(db_path: Path, kind: str, *, include_no_chat=False, shop=None) -> tuple[list[dict], dict]:
    """Only stored baselines and actual reasons; never infer a missing reason."""
    connection = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    skipped = Counter()
    candidates, fallback, seen = [], [], set()
    try:
        if kind == 'negative':
            rows = connection.execute("SELECT * FROM orders WHERE TRIM(COALESCE(appeal_text,''))<>'' ORDER BY COALESCE(submitted_at,analyzed_at,updated_at) DESC,shop,order_no").fetchall()
        else:
            table, baseline = ('reconsideration_records', 'reconsider_text') if kind == 'reconsideration' else ('aftersale_appeal_records', 'appeal_text')
            rows = connection.execute(f"SELECT * FROM {table} WHERE TRIM(COALESCE({baseline},''))<>'' ORDER BY COALESCE(submitted_at,updated_at) DESC,shop,order_no,record_key").fetchall()
        for row in rows:
            source = dict(row)
            if shop and source['shop'] != shop:
                continue
            key = (source['shop'], source['order_no'])
            if key in seen:
                skipped['同订单其他记录'] += 1
                continue
            reason = source.get('reconsider_reason', '') if kind == 'reconsideration' else source.get('appeal_reason', '') if kind == 'aftersale' else ''
            item = source.get('appeal_item', '') if kind == 'aftersale' else source.get('issue_type', '') or ''
            if kind != 'negative' and not str(reason or '').strip():
                skipped['未保存实际原因'] += 1
                continue
            if kind == 'aftersale' and not str(item or '').strip():
                skipped['未保存申诉项'] += 1
                continue
            history = source.get('reconsider_text', '') if kind == 'reconsideration' else source.get('appeal_text', '')
            own = _messages(source.get('messages'))
            if own is None:
                skipped['聊天格式错误'] += 1
                continue
            messages = own
            origin = '本流程记录'
            original_order = None
            if kind != 'negative':
                original_order = connection.execute('SELECT messages,classification,goods_title FROM orders WHERE shop=? AND order_no=?', key).fetchone()
                if not _buyer(messages) and original_order:
                    recovered = _messages(original_order['messages'])
                    if recovered and _buyer(recovered):
                        messages, origin = recovered, '同店铺同订单orders存档'
            text = '\n'.join(m['content'] for m in messages)
            embedded = ORDER_ID.findall(text)
            if any(number != source['order_no'] for number in embedded):
                skipped['聊天含其他订单号'] += 1
                continue
            goods = source.get('goods_title') or (original_order['goods_title'] if original_order else '') or ''
            if re.search('左耳|右耳', text) and goods and not re.search('音频|耳机|声卡|音响|AUX', goods, re.I):
                skipped['商品与聊天明显冲突'] += 1
                continue
            has_buyer = _buyer(messages)
            if messages and not has_buyer:
                skipped['无有效买家发言'] += 1
                continue
            record = {
                'shop': source['shop'], 'order_no': source['order_no'], 'record_key': source.get('record_key', ''),
                'history_text': history, 'history_status': source.get('status', ''),
                'submitted_at': source.get('submitted_at'), 'reason': reason or '', 'appeal_item': item,
                'classification': source.get('classification') or (original_order['classification'] if original_order else '') or '',
                'goods_title': goods, 'messages': messages, 'chat_source': origin,
                'mode': '模型生成' if has_buyer else '缺聊天兜底',
                'association': '聊天内订单号已匹配' if embedded else '仅按本地同店铺同订单关联，聊天内未见订单号' if messages else '缺少聊天，无法核验对话归属',
            }
            if has_buyer:
                candidates.append(record)
            elif include_no_chat:
                fallback.append(record)
            else:
                skipped['无聊天（可启用兜底）'] += 1
                continue
            seen.add(key)
    finally:
        connection.close()
    records = candidates + fallback
    groups = defaultdict(list)
    for index, record in enumerate(records):
        if record['messages']:
            groups[digest(json.dumps([(m.get('time'), m['sender'], m['content']) for m in record['messages']], ensure_ascii=False))].append(index)
    for group in groups.values():
        if len(group) > 1:
            for index in group:
                records[index]['duplicate_orders'] = [records[i]['order_no'] for i in group if i != index]
    return records, {'available': len(records), 'with_chat': len(candidates), 'fallback': len(fallback), 'skipped': dict(skipped)}


def load_adapter(kind: str):
    classifier = importlib.import_module('appeal_classifier')
    if kind == 'negative':
        owner, attribute, generate = classifier, 'APPEAL_TEXT_SYSTEM_PROMPT', classifier.generate_appeal_text
    elif kind == 'reconsideration':
        owner = importlib.import_module('appeal_wording')
        attribute, generate = 'RECONSIDERATION_DESCRIPTION_PROMPT', owner.generate_reconsideration_description
    else:
        owner = importlib.import_module('aftersale_appeal')
        attribute, generate = 'AFTERSALE_DESCRIPTION_PROMPT', owner.generate_aftersale_description
    return SimpleNamespace(classifier=classifier, owner=owner, attribute=attribute, generate=generate)


@contextmanager
def isolated_adapter(kind: str, effective_prompt: str, client):
    adapter = load_adapter(kind)
    style = importlib.import_module('appeal_style')
    saved_prompt = getattr(adapter.owner, adapter.attribute)
    saved_client = adapter.classifier._client
    saved_recent, saved_sequence = list(style._recent_wording), style._wording_sequence
    calls = []

    def capture(**kwargs):
        response = client.chat.completions.create(**kwargs)
        calls.append({k: kwargs.get(k) for k in ('model', 'messages', 'temperature', 'max_tokens', 'max_completion_tokens', 'timeout', 'extra_body')} | {
            'raw_text': response.choices[0].message.content,
            'finish_reason': response.choices[0].finish_reason,
            'response_model': response.model,
            'usage': response.usage.model_dump() if response.usage else None,
        })
        return response

    setattr(adapter.owner, adapter.attribute, effective_prompt)
    adapter.classifier._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=capture)))
    adapter.calls = calls
    try:
        yield adapter, style
    finally:
        setattr(adapter.owner, adapter.attribute, saved_prompt)
        adapter.classifier._client = saved_client
        style._recent_wording.clear()
        style._recent_wording.extend(saved_recent)
        style._wording_sequence = saved_sequence


def generate_record(adapter, kind: str, record: dict) -> str:
    compact = adapter.classifier.compact_chat_json({'order_no': record['order_no'], 'messages': record['messages']})
    if kind == 'negative':
        if not compact['m']:
            return adapter.classifier.get_no_chat_appeal_text(record['order_no'])
        return adapter.generate(compact, record['classification'])
    if kind == 'reconsideration':
        return adapter.generate(compact, record['reason'])
    return adapter.generate(compact if compact['m'] else None, record['classification'], record['appeal_item'], record['reason'])


def automatic_notes(kind: str, record: dict) -> list[str]:
    result = record.get('result', {})
    text = result.get('text', '')
    calls = result.get('calls', [])
    notes = []
    if record['mode'] == '缺聊天兜底':
        notes.append('无聊天固定兜底，不计为模型生成。')
    if any(c.get('raw_text') and c['raw_text'].strip() != text and c['raw_text'].strip().startswith(text) and len(c['raw_text'].strip()) > LIMITS[kind] for c in calls):
        notes.append('正式函数已按字数上限截断，核对是否丢失结论或出现残句。')
    if any(c.get('finish_reason') == 'length' for c in calls):
        notes.append('API输出达到token上限，检查完整性。')
    if '\n\n' in text or re.search(r'版本[一二12]|第[一二12]版', text):
        notes.append('返回文本含多段或版本标记，未擅自选择、拆分或改写。')
    if any(m['image_urls'] or re.search(r'\[图片\]|\[视频\]', m['content']) for m in record['messages']):
        notes.append('原材料含媒体，未识别内容，不能据标记认定凭证无效。')
    if kind != 'aftersale' and '运费' in text:
        notes.append('输出涉及运费，需核对是否超出本次申诉范围。')
    if record.get('duplicate_orders'):
        notes.append('聊天内容及时间与其他订单完全相同，核对采集归属：' + '、'.join(record['duplicate_orders']))
    if not record['goods_title']:
        notes.append('本地未保存商品标题。')
    if result.get('status') == 'error':
        notes.append('生成失败：' + result['error'])
    return notes


def runtime_paths(runtime_root=None):
    base = Path(runtime_root).expanduser().resolve() if runtime_root else Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies'
    node = base / 'node/bin/node.exe' if os.name == 'nt' else base / 'node/bin/node'
    modules = base / 'node/node_modules'
    if not node.is_file() or not (modules / '@oai/artifact-tool/package.json').is_file():
        raise RuntimeError('未找到Codex内置Excel运行时；用 --runtime-root 指定包含node目录的dependencies文件夹。尚未调用模型。')
    return node, modules


def export_workbook(run_file: Path, runtime_root=None) -> Path:
    node, modules = runtime_paths(runtime_root)
    workbook = run_file.parent / '话术对比.xlsx'
    skill_base = Path.home() / '.codex/plugins/cache/openai-primary-runtime/spreadsheets'
    markers = sorted(skill_base.glob('*/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs'))
    if markers:
        subprocess.run([str(node), str(markers[-1]), '--operation-kind', 'edit' if workbook.exists() else 'create', '--expected-output-count', '1', '--output-format', 'xlsx'], check=True, capture_output=True)
    builder = Path(__file__).with_name('export_comparison.mjs')
    command = [str(node), str(builder), str(run_file), str(modules)]
    exported = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if exported.returncode:
        raise RuntimeError('Excel导出失败，原始结果已保留；可使用 --export-only 重新导出。' + exported.stderr[-500:])
    verify_workbook(workbook, json.loads(run_file.read_text(encoding='utf-8')))
    previewed = subprocess.run(command + ['--preview'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if previewed.returncode:
        previewed = subprocess.run(command + ['--preview'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if previewed.returncode:
        print('提示：Excel已导出并核对，预览渲染失败；可用 --export-only 重试预览。')
    return workbook


def verify_workbook(path: Path, data: dict) -> None:
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(t.text or '' for t in a.iter('{' + ns['s'] + '}t')) for a in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        def value(cell):
            if cell.get('t') == 'inlineStr':
                return ''.join(t.text or '' for t in cell.findall('.//s:t', ns))
            v = cell.find('s:v', ns)
            return strings[int(v.text)] if cell.get('t') == 's' and v is not None else v.text if v is not None else ''
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cells = {c.get('r'): value(c) for c in sheet.findall('.//s:c', ns)}
        for index, record in enumerate(data['records'], 6):
            for col, expected in [('C', record['order_no']), ('F', record['history_text']), ('G', record.get('result', {}).get('text', ''))]:
                actual = cells.get(f'{col}{index}', '')
                quoted_literal = bool(re.match(r'^[=+@-]', expected)) and actual == "'" + expected
                if actual != expected and not quoted_literal:
                    raise RuntimeError(f'导出后文字校验不一致：{col}{index}')
        if len(sheet.findall('.//s:f', ns)) != len(data['records']) * 2:
            raise RuntimeError('字数公式未完整导出。')
        for name in archive.namelist():
            if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', name):
                if ET.fromstring(archive.read(name)).findall('.//s:c[@t="e"]', ns):
                    raise RuntimeError('Excel包含公式错误。')


def parser_for(kind):
    parser = argparse.ArgumentParser(description=f'{KINDS[kind]}提示词离线测试：只读数据库，不打开浏览器、不提交申诉。')
    parser.add_argument('--count', type=int, choices=(20, 50), help='目标订单数，默认20；不足默认停止且不调用API')
    parser.add_argument('--db', type=Path, default=ROOT / 'state/pddchat.db')
    parser.add_argument('--shop', help='只选择指定店铺，必须使用数据库中的完整店铺名')
    parser.add_argument('--prompt-file', type=Path, help='UTF-8文本，完整替换本进程的系统提示词，不修改正式文件')
    parser.add_argument('--allow-fewer', action='store_true', help='材料不足时按实际可用数量测试，明确记录不足')
    parser.add_argument('--include-no-chat', action='store_true', help='允许缺聊天记录走正式兜底；仍须有旧话术和实际原因')
    parser.add_argument('--list-only', action='store_true', help='只查看本地可用数量，不调用API、不导出')
    parser.add_argument('--output-root', type=Path, default=ROOT / 'outputs/prompt_tests')
    exclusive = parser.add_mutually_exclusive_group()
    exclusive.add_argument('--resume', type=Path, help='继续已有运行目录，跳过成功记录，重试失败记录')
    exclusive.add_argument('--export-only', type=Path, help='从已有运行目录重新导出Excel，不调用API')
    parser.add_argument('--runtime-root', type=Path, help='Codex内置dependencies目录，通常无需填写')
    return parser


def main(kind: str, argv=None) -> int:
    args = parser_for(kind).parse_args(argv)
    try:
        return run(kind, args)
    except KeyboardInterrupt:
        print('\n已停止，已完成结果保存在运行目录，可用 --resume 继续。')
        return 130
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as error:
        print('错误：' + str(error), file=sys.stderr)
        return 2


def run(kind, args):
    adapter = load_adapter(kind)
    production_prompt = getattr(adapter.owner, adapter.attribute)
    prompt_path = args.prompt_file.resolve() if args.prompt_file else None
    if args.export_only or args.resume:
        folder = (args.export_only or args.resume).resolve()
        run_file = folder / 'run.json'
        data = json.loads(run_file.read_text(encoding='utf-8'))
        if data['kind'] != kind:
            raise RuntimeError('运行目录的申诉类型与当前脚本不一致。')
        if args.list_only:
            print(f'已有运行：{data["actual_count"]}单；' + json.dumps(dict(Counter(r.get('result', {}).get('status', 'pending') for r in data['records'])), ensure_ascii=False))
            return 0
        if args.export_only:
            print('Excel：' + str(export_workbook(run_file, args.runtime_root)))
            return 0
        if args.count is not None and args.count != data['requested_count']:
            raise RuntimeError('断点续跑不能改变订单数量；改变数量请新建一轮测试。')
        for name, expected in data['source_hashes'].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError('正式生成代码或提示词已变化；请新建一轮测试，不混用断点结果。')
        if prompt_path is None and data.get('prompt_file'):
            prompt_path = Path(data['prompt_file'])
        prompt = prompt_path.read_text(encoding='utf-8-sig') if prompt_path else production_prompt
        if digest(prompt) != data['prompt_sha256']:
            raise RuntimeError('测试提示词已变化；请新建一轮测试。')
    else:
        records, selection = select_records(args.db, kind, include_no_chat=args.include_no_chat, shop=args.shop)
        count = args.count or 20
        print(f'可用{selection["available"]}单：有买家聊天{selection["with_chat"]}单，缺聊天兜底{selection["fallback"]}单。')
        print('筛选跳过：' + json.dumps(selection['skipped'], ensure_ascii=False))
        if args.list_only:
            return 0
        if selection['available'] < count and not args.allow_fewer:
            raise RuntimeError(f'目标{count}单，当前只有{selection["available"]}单。尚未调用API。可加 --allow-fewer 按实际数量测试，或 --include-no-chat 纳入缺聊天兜底。')
        records = records[:count]
        if not records:
            raise RuntimeError('没有可测试的历史对比记录。')
        prompt = prompt_path.read_text(encoding='utf-8-sig') if prompt_path else production_prompt
        if not prompt.strip():
            raise RuntimeError('提示词文件为空。')
        runtime_paths(args.runtime_root)  # Fail before spending API tokens.
        label = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
        folder = args.output_root.resolve() / f'{kind}_{label}_{uuid.uuid4().hex[:6]}'
        folder.mkdir(parents=True, exist_ok=False)
        run_file = folder / 'run.json'
        data = {
            'schema_version': 1, 'kind': kind, 'title': KINDS[kind], 'created_at': now(),
            'requested_count': count, 'actual_count': len(records), 'selection': selection,
            'database': str(args.db.resolve()), 'prompt_file': str(prompt_path) if prompt_path else None,
            'production_prompt': production_prompt, 'effective_prompt': prompt, 'prompt_sha256': digest(prompt),
            'source_hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES[kind]},
            'records': records, 'recent_wording': [], 'wording_sequence': 0,
        }
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        save_run(run_file, data)
    print('运行目录：' + str(folder))
    pending = [r for r in data['records'] if r.get('result', {}).get('status') not in ('success', 'fallback')]
    runtime_paths(args.runtime_root)
    client = adapter.classifier.get_client() if any(r['mode'] == '模型生成' for r in pending) else None
    try:
        with isolated_adapter(kind, prompt, client) as (active, style):
            style._recent_wording.clear(); style._recent_wording.extend(data['recent_wording'])
            style._wording_sequence = data['wording_sequence']
            for index, record in enumerate(data['records'], 1):
                if record.get('result', {}).get('status') in ('success', 'fallback'):
                    continue
                if record.get('result'):
                    record.setdefault('previous_attempts', []).append(record['result'])
                active.calls.clear()
                start = time.perf_counter()
                try:
                    text = generate_record(active, kind, record)
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError('EmptyOutput')
                    if len(text) > LIMITS[kind]:
                        raise ValueError('OutputOverBusinessLimit')
                    result = {'status': 'fallback' if record['mode'] == '缺聊天兜底' else 'success', 'text': text}
                except Exception as error:
                    # Provider error bodies can echo credentials. Keep only safe metadata.
                    result = {'status': 'error', 'text': '', 'error': type(error).__name__, 'http_status': getattr(error, 'status_code', None)}
                result.update(seconds=round(time.perf_counter() - start, 2), calls=list(active.calls), tested_at=now())
                record['result'] = result
                record['notes'] = automatic_notes(kind, record)
                data['recent_wording'] = list(style._recent_wording)
                data['wording_sequence'] = style._wording_sequence
                save_run(run_file, data)
                print(f'{index}/{len(data["records"])} {record["order_no"]} {result["status"]} 字数={len(result["text"])} 调用={len(active.calls)}', flush=True)
                if result['text']:
                    print('最终话术：' + result['text'], flush=True)
                else:
                    print(f'生成失败：{result["error"]}；HTTP状态={result.get("http_status")}', flush=True)
                if result.get('http_status') in (401, 403):
                    print('接口认证、权限或额度异常，停止后续调用；已保存结果，修复后可 --resume 续跑。')
                    break
    finally:
        if client is not None:
            client.close()
        adapter.classifier._client = None  # A closed SDK client must not be reused.
    workbook = export_workbook(run_file, args.runtime_root)
    errors = sum(r.get('result', {}).get('status') == 'error' for r in data['records'])
    print('Excel：' + str(workbook))
    print('提示词快照：' + str(folder / 'prompt.txt'))
    if errors:
        print(f'{errors}单生成失败，已在Excel标明；修复接口后用 --resume "{folder}" 重试。')
    return 1 if errors else 0
