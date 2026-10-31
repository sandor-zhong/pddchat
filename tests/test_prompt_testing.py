import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prompt_testing import runner


def message(text='充不进电', role='B'):
    return {'sender': '1***' if role == 'B' else '客服', 'content': text,
            'is_system_message': False, 'image_urls': [], 'time': '2026-09-01 10:00:00'}


def database(path):
    with contextlib.closing(sqlite3.connect(path)) as c, c:
        c.executescript('''
CREATE TABLE orders(shop TEXT,order_no TEXT,messages TEXT,classification TEXT,goods_title TEXT,appeal_text TEXT,status TEXT,submitted_at TEXT,analyzed_at TEXT,updated_at TEXT);
CREATE TABLE reconsideration_records(shop TEXT,order_no TEXT,record_key TEXT,reconsider_reason TEXT,reconsider_text TEXT,status TEXT,goods_title TEXT,submitted_at TEXT,updated_at TEXT);
CREATE TABLE aftersale_appeal_records(shop TEXT,order_no TEXT,record_key TEXT,appeal_item TEXT,appeal_reason TEXT,appeal_text TEXT,messages TEXT,classification TEXT,goods_title TEXT,status TEXT,submitted_at TEXT,updated_at TEXT);
''')
        for number in ('260901-123456789012345', '260902-123456789012345'):
            c.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)', ('店A', number, json.dumps([message()]), '原分类', '充电线', '负向旧文', 'submitted', '2026-10-01', '2026-10-01', '2026-10-01'))
            c.execute('INSERT INTO reconsideration_records VALUES(?,?,?,?,?,?,?,?,?)', ('店A', number, 'rec-'+number, '实际复议原因', '复议旧文', '申诉失败', '充电线', '2026-10-01', '2026-10-01'))
            c.execute('INSERT INTO aftersale_appeal_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('店A', number, 'after-'+number, '运费申诉', '实际售后原因', '售后旧文', '[]', '原分类', '充电线', '已提交', '2026-10-01', '2026-10-01'))


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason='stop')], model='test-mock', usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, model_dump=lambda: {'prompt_tokens':1, 'completion_tokens':2}))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        database(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_baselines_and_reasons_are_from_the_correct_business_table(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        for kind, old in [('negative','负向旧文'),('reconsideration','复议旧文'),('aftersale','售后旧文')]:
            rows, _ = runner.select_records(self.path, kind)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['history_text'], old)
            if kind == 'reconsideration':
                self.assertEqual(rows[0]['reason'], '实际复议原因')
                self.assertEqual(rows[0]['chat_source'], '同店铺同订单orders存档')
            if kind == 'aftersale':
                self.assertEqual(rows[0]['reason'], '实际售后原因')
                self.assertEqual(rows[0]['appeal_item'], '运费申诉')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_missing_reason_and_wrong_order_chat_are_not_replaced_or_mixed(self):
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE aftersale_appeal_records SET appeal_reason='' WHERE order_no='260901-123456789012345'")
            c.execute('UPDATE orders SET messages=? WHERE order_no=?', (json.dumps([message('订单编号:260999-987654321098765')]), '260902-123456789012345'))
        rows, stats = runner.select_records(self.path, 'aftersale')
        self.assertEqual(rows, [])
        self.assertEqual(stats['skipped']['未保存实际原因'], 1)
        self.assertEqual(stats['skipped']['聊天含其他订单号'], 1)

    def test_no_cross_shop_recovery_and_fallback_is_opt_in(self):
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE aftersale_appeal_records SET shop='店B'")
        rows, _ = runner.select_records(self.path, 'aftersale')
        self.assertEqual(rows, [])
        rows, stats = runner.select_records(self.path, 'aftersale', include_no_chat=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['mode'] == '缺聊天兜底' for r in rows))
        self.assertEqual(stats['with_chat'], 0)

    def test_shortage_stops_before_api_or_output_creation(self):
        destination = Path(self.tmp.name)/'outputs'
        with patch('appeal_classifier.get_client') as client, patch.object(runner, 'runtime_paths') as runtime, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = runner.main('aftersale', ['--db', str(self.path), '--count', '20', '--output-root', str(destination)])
        self.assertEqual(code, 2)
        client.assert_not_called(); runtime.assert_not_called()
        self.assertFalse(destination.exists())


class ProductionAdapterTests(unittest.TestCase):
    def test_auth_failure_saves_safe_partial_run_and_resume_list_spends_no_tokens(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / 'test.db'
            database(db)
            failure = RuntimeError('SECRET_PROVIDER_BODY')
            failure.status_code = 403
            client = SimpleNamespace(close=unittest.mock.Mock())
            with patch.object(runner, 'runtime_paths'), patch.object(runner, 'export_workbook', return_value=root/'test.xlsx'), patch('appeal_classifier.get_client', return_value=client), patch.object(runner, 'generate_record', side_effect=failure) as generate, contextlib.redirect_stdout(io.StringIO()):
                code = runner.main('negative', ['--db',str(db),'--allow-fewer','--output-root',str(root/'runs')])
            self.assertEqual(code, 1)
            self.assertEqual(generate.call_count, 1)
            folder = next((root/'runs').iterdir())
            serialized = (folder/'run.json').read_text(encoding='utf-8')
            self.assertNotIn('SECRET_PROVIDER_BODY', serialized)
            saved = json.loads(serialized)
            self.assertEqual(saved['records'][0]['result']['http_status'],403)
            self.assertNotIn('result', saved['records'][1])
            client.close.assert_called_once()
            with patch('appeal_classifier.get_client') as get_client, patch.object(runner,'export_workbook') as export, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main('negative',['--resume',str(folder),'--list-only']),0)
                get_client.assert_not_called()
                export.assert_not_called()
                self.assertEqual(runner.main('negative',['--export-only',str(folder)]),0)
                get_client.assert_not_called()
                export.assert_called_once()
            with patch('appeal_classifier.get_client') as get_client, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main('negative',['--resume',str(folder),'--count','50']),2)
                get_client.assert_not_called()
                saved['prompt_sha256']='changed'
                (folder/'run.json').write_text(json.dumps(saved),encoding='utf-8')
                self.assertEqual(runner.main('negative',['--resume',str(folder)]),2)
                get_client.assert_not_called()

    def test_custom_prompt_is_temporary_and_old_text_is_never_model_evidence(self):
        for kind in runner.KINDS:
            module = runner.load_adapter(kind)
            original = getattr(module.owner, module.attribute)
            create = unittest.mock.Mock(return_value=response('只按本单真实记录生成。'))
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            record = {'order_no':'260901-123456789012345','messages':[message()], 'classification':'原分类','reason':'已保存原因','appeal_item':'货款申诉','history_text':'SECRET_OLD_BASELINE_NOT_EVIDENCE'}
            with runner.isolated_adapter(kind, '自定义测试提示词', client) as (active, style), contextlib.redirect_stdout(io.StringIO()):
                style._recent_wording.clear(); style._wording_sequence = 0
                text = runner.generate_record(active, kind, record)
                self.assertEqual(text, '只按本单真实记录生成。')
                self.assertEqual(active.calls[0]['messages'][0]['content'], '自定义测试提示词')
                serialized = json.dumps(active.calls[0]['messages'], ensure_ascii=False)
                self.assertNotIn('SECRET_OLD_BASELINE_NOT_EVIDENCE', serialized)
                self.assertIn('充不进电', serialized)
            self.assertEqual(getattr(module.owner, module.attribute), original)

    def test_no_chat_uses_each_business_fallback_without_api_calls(self):
        create = unittest.mock.Mock(side_effect=AssertionError('No API expected'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        for kind in runner.KINDS:
            r = {'order_no':'260901-123456789012345','messages':[], 'classification':'', 'reason':'实际原因', 'appeal_item':'运费申诉'}
            with runner.isolated_adapter(kind, '测试提示词', client) as (active, _):
                self.assertTrue(runner.generate_record(active, kind, r))
                self.assertEqual(active.calls, [])
        create.assert_not_called()

    def test_negative_truncation_and_candidates_remain_distinguishable(self):
        raw = '真实订单的处理经过。' * 20
        create = unittest.mock.Mock(return_value=response(raw))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        r = {'order_no':'260901-123456789012345','messages':[message()], 'classification':'原分类','mode':'模型生成','goods_title':'充电线'}
        with runner.isolated_adapter('negative','测试',client) as (active,_), contextlib.redirect_stdout(io.StringIO()):
            text = runner.generate_record(active,'negative',r)
            self.assertLessEqual(len(text),120)
            self.assertEqual(active.calls[0]['raw_text'],raw)
            r['result']={'text':text,'calls':list(active.calls),'status':'success'}
            self.assertTrue(any('截断' in note for note in runner.automatic_notes('negative',r)))

    def test_failed_candidate_restores_prompt_client_and_style_state(self):
        import appeal_style as style
        module = runner.load_adapter('aftersale')
        old = getattr(module.owner,module.attribute)
        prior = list(style._recent_wording), style._wording_sequence
        create = unittest.mock.Mock(side_effect=RuntimeError('provider failure'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(RuntimeError):
            with runner.isolated_adapter('aftersale','TEMP',client) as (active,_):
                style._recent_wording.append('temporary');style._wording_sequence+=1
                runner.generate_record(active,'aftersale',{'order_no':'x','messages':[message()], 'classification':'','reason':'真实原因','appeal_item':'货款申诉'})
        self.assertEqual(getattr(module.owner,module.attribute),old)
        self.assertEqual((list(style._recent_wording),style._wording_sequence),prior)


if __name__ == '__main__':
    unittest.main()
