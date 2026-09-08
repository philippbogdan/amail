import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest.mock import patch
import test_mail as fixtures
from configuration import setup, load, save, apply, migrate_legacy, validate
from local_store import MailError
from policy import profile
from install import install


class SetupTests(unittest.TestCase):
    setUp = fixtures.Fixture.setUp
    write_message = fixtures.Fixture.write_message

    def args(self, **values):
        return argparse.Namespace(**dict({'accounts':None,'name':None,'gmail_client_json':None,'connect':False}, **values))

    def test_discovery_never_enables_accounts_or_sends(self):
        state=self.base/'state'
        with patch('subprocess.run',side_effect=AssertionError('external call')):
            result=setup(self.args(),state,store_factory=lambda:self.store)
        self.assertEqual(len(result['available_accounts']),1)
        self.assertFalse((state/'config.json').exists())

    def test_no_config_or_empty_selection_never_enables_all_accounts(self):
        with self.assertRaises(MailError):load(self.base/'missing')
        for value in [{}, {'accounts':[]}, {'accounts':['*']}]:
            with self.assertRaises(MailError):validate(value)

    def test_setup_selects_account_and_preserves_user_preferences(self):
        state=self.base/'state'
        save({'accounts':['owner@example.com'],'display_name':'Existing Name','policies':{'gmail':{'recipients_per_day':30}}},state)
        with patch('subprocess.run',side_effect=AssertionError('external call')):
            setup(self.args(accounts=['alias@example.com']),state,store_factory=lambda:self.store)
        result=load(state)
        self.assertEqual(result['accounts'],['owner@example.com'])
        self.assertEqual(result['display_name'],'Existing Name')
        self.assertEqual(result['policies']['gmail']['recipients_per_day'],30)
        self.assertEqual(stat.S_IMODE((state/'config.json').stat().st_mode),0o600)

    def test_setup_rejects_unknown_account_without_changing_config(self):
        state=self.base/'state';save({'accounts':['owner@example.com']},state)
        before=(state/'config.json').read_bytes()
        with self.assertRaises(MailError):setup(self.args(accounts=['stranger@example.net']),state,store_factory=lambda:self.store)
        self.assertEqual((state/'config.json').read_bytes(),before)

    def test_selected_account_removed_from_mail_fails_closed(self):
        value=validate({'accounts':['owner@example.com','removed@example.net']})
        with self.assertRaises(MailError):apply(self.store,value)

    def test_policy_overrides_apply_to_real_send_reservations(self):
        from mail_sender import ledger
        from policy import reserve, WaitRequired
        state=self.base/'state';value=validate({'accounts':['owner@example.com'],'policies':{'gmail':{'recipients_per_day':1}}})
        store=apply(self.store,value);account=store.resolve('owner@example.com')[0]
        self.assertEqual(profile(account)['recipients_per_day'],1)
        with ledger(state) as c,c:
            c.execute("insert into sends(request_id,account,created,state,recipients,purpose) values('used','GMAIL',1000,'accepted',1,'outreach')")
            with self.assertRaises(WaitRequired):reserve(c,account,1,['next@example.net'],'outreach',now=1001)

    def test_invalid_policy_rejected(self):
        for rule in [{'gap_min_seconds':0},{'gap_max_seconds':1},{'recipients_per_day':1.5},{'window_seconds':True},{'recipients_per_day':float('inf')},{'unknown':1}]:
            with self.assertRaises(MailError):validate({'accounts':['owner@example.com'],'policies':{'gmail':rule}})

    def test_credential_registration_uses_file_and_named_client(self):
        state=self.base/'state';download=self.base/'client.json';download.write_text(json.dumps({'installed':{'client_id':'fixture-client','client_secret':'fixture-value'}}))
        stored=self.base/'gog/credentials-amail.json'
        def register(argv,**kwargs):
            self.assertNotIn('fixture-value',' '.join(argv))
            self.assertEqual(argv,['gog','auth','credentials','set',str(download.resolve()),'--client','amail'])
            stored.parent.mkdir();stored.write_text('{}')
            return subprocess.CompletedProcess(argv,0,'','')
        with patch('configuration.credentials_path',return_value=stored),patch('subprocess.run',side_effect=register):
            result=setup(self.args(accounts=['owner@example.com'],gmail_client_json=str(download)),state,store_factory=lambda:self.store)
        self.assertEqual(result['state'],'configured')
        self.assertNotIn('fixture-value',(state/'config.json').read_text())

    def test_invalid_credentials_never_call_gog(self):
        download=self.base/'invalid.json';download.write_text('{}')
        with patch('subprocess.run',side_effect=AssertionError('external call')):
            with self.assertRaises(MailError):setup(self.args(accounts=['owner@example.com'],gmail_client_json=str(download)),self.base/'state',store_factory=lambda:self.store)

    def test_migrate_legacy_keeps_accounts_name_policy_and_secret_storage(self):
        old=self.base/'old';old.mkdir();(old/'amail').write_text('# legacy placeholder\n')
        (old/'accounts.json').write_text(json.dumps({'accounts':['owner@example.com']}))
        (old/'cli.py').write_text("p.add_argument('--name', default='Existing Name')\n")
        (old/'policy.py').write_text("PROFILES={'gmail':{'recipients_per_day':30},'institution':{'recipients_per_day':400,'messages_per_window':50,'window_seconds':600,'gap_min_seconds':5,'gap_max_seconds':10}}\n")
        state=self.base/'state';state.mkdir();(state/'sends.sqlite').write_bytes(b'unchanged-history');(state/'auth').mkdir();(state/'auth/cache.json').write_text('private cached token')
        executable=self.base/'bin/amail';executable.parent.mkdir();executable.symlink_to(old/'amail')
        self.assertTrue(migrate_legacy(executable,state))
        migrated=load(state)
        self.assertEqual(migrated['accounts'],['owner@example.com'])
        self.assertEqual(migrated['display_name'],'Existing Name')
        self.assertEqual(migrated['policies']['exchange']['recipients_per_day'],400)
        self.assertEqual((state/'sends.sqlite').read_bytes(),b'unchanged-history')
        self.assertEqual((state/'auth/cache.json').read_text(),'private cached token')
        self.assertFalse(migrate_legacy(executable,state))

    def test_isolated_install_without_mail_access_and_repeat_upgrade(self):
        state=self.base/'state';prefix=self.base/'prefix'
        with patch.dict(os.environ,{'AMAIL_STATE_DIR':str(state),'AMAIL_CONFIG':str(state/'config.json')}):
            result=install(prefix)
            self.assertFalse(result['legacy_settings_migrated'])
            self.assertFalse((state/'config.json').exists())
            command=prefix/'bin/amail';p=subprocess.run([str(command),'--version'],capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0);self.assertIn('0.2.1',p.stdout)
            p=subprocess.run([str(command),'accounts'],capture_output=True,text=True,timeout=10)
            self.assertNotEqual(p.returncode,0);self.assertIn('amail setup',p.stderr)
            save({'accounts':['owner@example.com'],'display_name':'Existing Name'},state)
            before=(state/'config.json').read_bytes();second=install(prefix)
            self.assertEqual(result['release'],second['release'])
            self.assertEqual((state/'config.json').read_bytes(),before)
            manifest=json.loads((Path(result['release'])/'manifest.json').read_text())
            self.assertNotIn('config.json',manifest)
            self.assertFalse((Path(result['release'])/'accounts.json').exists())


if __name__ == '__main__': unittest.main()
