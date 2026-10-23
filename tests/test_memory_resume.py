import io
import json
import unittest
from unittest.mock import MagicMock, patch

import main
from aftersale_appeal import AftersaleAppealSession


class ResumeTests(unittest.TestCase):
    def test_normal_appeal_resume_uses_only_saved_remaining_queue(self):
        commands=[{'cmd':'init','resume':{'order_numbers':['remaining-2','remaining-3'],'order_issues':{}}},
                  {'cmd':'next'},{'cmd':'stop'}]
        output=io.StringIO()
        context=MagicMock()
        with patch('main.sys.stdin',io.StringIO('\n'.join(json.dumps(c) for c in commands))), \
             patch('main.create_stealth_context',return_value=context), \
             patch('main.wait_for_login'), patch('main._get_or_create_role_page'), \
             patch('main.set_give_up_after_auto_solve_fails'), patch('main.run_phase1') as scan, \
             patch('main.process_and_submit_one_order',return_value=(None,'已提交')) as process:
            main.run_serve_mode(MagicMock(),output,confirm=True,reanalyze=False)
        scan.assert_not_called()
        self.assertEqual(process.call_args.args[2],'remaining-2')
        messages=[json.loads(line) for line in output.getvalue().splitlines()]
        activity=next(message for message in messages if message['type']=='activity')
        self.assertEqual(activity['order_no'],'remaining-2')
        result=next(message for message in messages if message['type']=='result')
        self.assertEqual(result['_resume']['order_numbers'],['remaining-3'])
        context.close.assert_called_once()

    def test_aftersale_restore_preserves_position_without_resubmitting(self):
        session=AftersaleAppealSession(MagicMock(),MagicMock(),MagicMock(),max_orders=50)
        state=dict(processed=25,page_number=1,tasks=[{'record_key':'done'},{'record_key':'next'}],
                   task_index=1,done=False,visited_signatures=['done|next'])
        with patch('aftersale_appeal.open_aftersale_appeal_page'), \
             patch('aftersale_appeal.wait_for_aftersale_list_ready',return_value='rows'), \
             patch('aftersale_appeal.extract_aftersale_rows',return_value=[{'record_key':'next'}]), \
             patch('aftersale_appeal.process_aftersale_task') as process:
            self.assertEqual(session.restore(state),25)
        self.assertEqual(session.task_index,1)
        self.assertEqual(session.processed,25)
        process.assert_not_called()

    def test_changed_aftersale_page_refuses_resume(self):
        session=AftersaleAppealSession(MagicMock(),MagicMock(),MagicMock(),max_orders=50)
        state=dict(processed=25,page_number=1,tasks=[{'record_key':'next'}],task_index=0,
                   done=False,visited_signatures=[])
        with patch('aftersale_appeal.open_aftersale_appeal_page'), \
             patch('aftersale_appeal.wait_for_aftersale_list_ready',return_value='rows'), \
             patch('aftersale_appeal.extract_aftersale_rows',return_value=[{'record_key':'different'}]):
            with self.assertRaisesRegex(RuntimeError,'避免错单'):
                session.restore(state)
        self.assertFalse(session.initialized)
