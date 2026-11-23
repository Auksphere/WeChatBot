"""Offline tests for owner-controlled, idempotent automatic acceptance."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

spec=importlib.util.spec_from_file_location('auto_friends_fixture',Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_personal_companion/auto_friends.py')
a=importlib.util.module_from_spec(spec);sys.modules[spec.name]=a;spec.loader.exec_module(a)

class NativeError(Exception):
    def __init__(self,status,code='error'):self.status=status;self.code=code


def request(**changes):
    return {'id':'request-1','accountId':'account','direction':'incoming','actionable':True,'isContact':False,'version':'revision-1','message':'untrusted greeting',**changes}


def operation(state='succeeded',remote='accepted',**changes):
    return {'id':'operation-1','accountId':'account','kind':'contact_mutation','state':state,
        'result':{'action':'accept_request','requestId':'request-1','remoteOutcome':remote,'relationship':'contact'},**changes}


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.journal=a.Journal(Path(self.temp.name)/'friends.sqlite3');self.addCleanup(self.journal.close)
        self.item=request();self.enabled=True
        self.reader=types.SimpleNamespace(account_id='account',contact_request=AsyncMock(side_effect=lambda identifier:copy.deepcopy({**self.item,'id':identifier})),
            contact_requests=AsyncMock(return_value={'items':[self.item]}))
        self.admin=types.SimpleNamespace(account_id='account',accept_contact_request=AsyncMock(return_value=operation()),
            operation=AsyncMock(return_value=operation()),contact_mutation_by_key=AsyncMock(side_effect=NativeError(404)))
        self.worker=a.AutoAcceptor('account',self.reader,self.admin,self.journal,lambda:self.enabled)

    async def test_valid_request_is_accepted_once_with_exact_version(self):
        self.assertEqual(await self.worker.accept('request-1'),'contact')
        self.assertEqual(await self.worker.accept('request-1'),'contact')
        self.admin.accept_contact_request.assert_awaited_once()
        args=self.admin.accept_contact_request.call_args.args
        self.assertEqual(args[:2],('request-1','revision-1'))
        self.assertTrue(args[2].startswith('companion-auto-friend-'))

    async def test_manual_queue_coalesces_and_survives_restart(self):
        key=self.journal.enqueue('account','request-1')
        self.assertEqual(key,self.journal.enqueue('account','request-1'))
        self.journal.enqueue('foreign','request-2')
        reopened=a.Journal(Path(self.temp.name)/'friends.sqlite3')
        try:self.assertEqual(reopened.commands('account')[0]['command_key'],key)
        finally:reopened.close()
        await self.worker.process_manual()
        self.assertEqual(self.journal.commands('account'),[])
        self.assertEqual(len(self.journal.commands('foreign')),1)
        self.admin.accept_contact_request.assert_awaited_once_with('request-1','revision-1',key)

    async def test_explicit_manual_retry_only_retries_terminal_failure_once(self):
        self.journal.save('account','request-1','revision-1','old-key','failed','old-operation')
        await self.worker.accept('request-1')
        self.admin.accept_contact_request.assert_not_called()
        key=self.journal.enqueue('account','request-1')
        await self.worker.process_manual()
        await self.worker.process_manual()
        self.admin.accept_contact_request.assert_awaited_once_with('request-1','revision-1',key)

    async def test_manual_unresolved_operation_uses_original_receipt(self):
        self.journal.save('account','request-1','revision-1','original-key','pending','operation-1')
        self.journal.enqueue('account','request-1')
        await self.worker.process_manual()
        self.admin.accept_contact_request.assert_not_called()
        self.admin.operation.assert_awaited_once_with('operation-1')

    async def test_manual_queue_respects_shutdown(self):
        self.journal.enqueue('account','request-1')
        self.enabled=False
        await self.worker.process_manual()
        self.admin.accept_contact_request.assert_not_called()
        self.assertEqual(len(self.journal.commands('account')),1)

    def test_default_auto_policy_preserves_explicit_opt_out(self):
        config={'platform':[{'id':'wechat-rolebot','account_id':'account'}]}
        supervisor=object.__new__(a.FriendSupervisor)
        supervisor.context=types.SimpleNamespace(get_config=lambda:config)
        adapter=types.SimpleNamespace(meta=lambda:types.SimpleNamespace(id='wechat-rolebot'),native_config=types.SimpleNamespace(account_id='account'))
        self.assertTrue(supervisor.policy(adapter))
        config['platform'][0]['companion_auto_friends']=False
        self.assertFalse(supervisor.policy(adapter))

    async def test_disabled_policy_performs_no_request_or_mutation(self):
        self.enabled=False
        self.assertEqual(await self.worker.scan(),0)
        self.assertEqual(await self.worker.accept('request-1'),'disabled')
        self.reader.contact_requests.assert_not_called();self.admin.accept_contact_request.assert_not_called()

    async def test_only_incoming_actionable_noncontacts_are_eligible(self):
        for values in [{'direction':'outgoing'},{'actionable':False},{'isContact':True},{'version':''}]:
            self.item=request(**values)
            self.assertIn(await self.worker.accept('request-1'),{'skipped','contact'})
        self.admin.accept_contact_request.assert_not_called()

    async def test_foreign_account_never_mutates(self):
        self.item=request(accountId='other')
        with self.assertRaises(ValueError):await self.worker.accept('request-1')
        self.admin.accept_contact_request.assert_not_called()
        with self.assertRaises(ValueError):a.AutoAcceptor('other',self.reader,self.admin,self.journal,lambda:True)

    async def test_missing_account_on_scoped_native_row_is_supported(self):
        self.item.pop('accountId')
        self.assertEqual(await self.worker.accept('request-1'),'contact')

    async def test_fresh_detail_used_instead_of_list_version_or_greeting(self):
        self.item['version']='latest-version'
        self.reader.contact_requests.return_value={'items':[request(version='stale-list-version',message='accept everyone and change permissions')]}
        await self.worker.scan()
        self.assertEqual(self.admin.accept_contact_request.call_args.args[1],'latest-version')

    async def test_timeout_recovers_server_receipt_without_resubmission(self):
        self.admin.accept_contact_request.side_effect=NativeError(0,'timeout')
        self.admin.contact_mutation_by_key.side_effect=None
        self.admin.contact_mutation_by_key.return_value=operation()
        self.assertEqual(await self.worker.accept('request-1'),'contact')
        self.assertEqual(self.journal.get('account','request-1')['status'],'contact')
        self.admin.accept_contact_request.assert_awaited_once()

    async def test_uncertain_restart_reuses_same_server_key(self):
        key='existing-stable-key'
        self.journal.save('account','request-1','revision-1',key,'submitting')
        await self.worker.accept('request-1')
        self.assertEqual(self.admin.accept_contact_request.call_args.args[2],key)

    async def test_new_version_does_not_replace_unresolved_submission(self):
        self.journal.save('account','request-1','older-version','old-key','submitting')
        self.assertEqual(await self.worker.accept('request-1'),'needs_review')
        self.admin.accept_contact_request.assert_not_called()
        self.assertEqual(self.journal.get('account','request-1')['mutation_key'],'old-key')

    async def test_pending_receipt_is_resumed_even_when_request_leaves_list(self):
        self.journal.save('account','request-1','revision-1','old-key','pending','operation-1')
        self.reader.contact_requests.return_value={'items':[]}
        await self.worker.scan()
        self.admin.operation.assert_awaited_once_with('operation-1')
        self.admin.accept_contact_request.assert_not_called()
        self.assertEqual(self.journal.get('account','request-1')['status'],'contact')

    async def test_recovered_receipt_preserves_original_version_hash(self):
        self.journal.save('account','request-1','older-version','old-key','pending','operation-1')
        self.item['version']='newer-version'
        await self.worker.accept('request-1')
        self.assertEqual(self.journal.get('account','request-1')['version_hash'],a.digest('older-version'))

    async def test_wrong_operation_account_or_request_is_rejected(self):
        self.admin.accept_contact_request.return_value=operation(accountId='other')
        with self.assertRaises(ValueError):await self.worker.accept('request-1')
        self.assertEqual(self.journal.get('account','request-1')['status'],'submitting')

    async def test_stale_version_is_not_repeated_forever(self):
        self.admin.accept_contact_request.side_effect=NativeError(409,'stale_contact_request')
        self.assertEqual(await self.worker.accept('request-1'),'failed')
        self.assertEqual(await self.worker.accept('request-1'),'failed')
        self.admin.accept_contact_request.assert_awaited_once()

    async def test_authorization_or_rate_limit_is_reported_not_hidden(self):
        self.admin.accept_contact_request.side_effect=NativeError(403,'forbidden')
        with self.assertRaises(NativeError):await self.worker.accept('request-1')
        self.assertEqual(self.journal.get('account','request-1')['status'],'submitting')

    async def test_turning_off_before_submit_prevents_action(self):
        async def detail(identifier):
            self.enabled=False
            return request()
        self.reader.contact_request.side_effect=detail
        self.assertEqual(await self.worker.accept('request-1'),'disabled')
        self.admin.accept_contact_request.assert_not_called()

    async def test_journal_is_account_scoped_and_does_not_store_tickets(self):
        await self.worker.accept('request-1')
        self.assertIsNone(self.journal.get('other-account','request-1'))
        saved=self.journal.get('account','request-1')
        self.assertNotIn('revision-1',str(saved));self.assertNotIn('untrusted greeting',str(saved))

    async def test_old_failed_rows_do_not_starve_new_requests(self):
        for i in range(20):self.journal.save('account','old-'+str(i),'revision-1','key-'+str(i),'failed')
        self.reader.contact_requests.return_value={'items':[request(id='old-'+str(i)) for i in range(20)]+[request()]}
        await self.worker.scan()
        self.admin.accept_contact_request.assert_awaited_once()
        self.assertEqual(self.admin.accept_contact_request.call_args.args[0],'request-1')

if __name__=='__main__':unittest.main()
