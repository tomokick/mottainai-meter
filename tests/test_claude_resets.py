import unittest
from unittest.mock import patch
import mottainai as m

class ClaudeResetTests(unittest.TestCase):
    def test_unsupported_surface_is_not_zero(self):
        d=m.claude_reset_details({'cedar_ember': {'eligible':False,'ineligible_reason':'surface','grants':[]}})
        self.assertIsNone(d['available'])
        self.assertEqual(d['status'],'unsupported')

    def test_missing_or_malformed_is_unknown(self):
        for raw in ({}, {'cedar_ember':None}, {'cedar_ember':{'eligible':True,'grants':[{}]}}):
            self.assertIsNone(m.claude_reset_details(raw)['available'])

    def test_explicit_no_grants(self):
        d=m.claude_reset_details({'cedar_ember':{'eligible':False,'ineligible_reason':'no_grant'}})
        self.assertEqual(d['available'],0)

    def test_counts_exclude_expired_paused_and_duplicates(self):
        def grant(i,n,**kw):
            return dict(id=i,resets_left=n,resets_total=3,ends_at='2030-01-01T00:00:00Z',**kw)
        expired=grant('expired',2);expired['ends_at']='2020-01-01T00:00:00Z'
        grants=[grant('a',2),grant('a',2),grant('paused',2,paused=True),expired]
        with patch.object(m,'now_ts',return_value=1793246400):
            d=m.claude_reset_details({'cedar_ember':{'eligible':True,'grants':grants}})
        self.assertEqual(d['available'],2)
        self.assertIsNotNone(d['expires_at'])

    def test_observation_cannot_override_live_status(self):
        with patch.object(m,'fetch_claude',return_value=[]), patch.object(m,'fetch_codex',return_value=([],None,{})), patch.object(m,'claude_reset_observation',return_value={'available':0,'checked_at':100}):
            *_, detail=m.collect({})
        self.assertIsNone(detail['available'])
        self.assertEqual(detail['observed']['available'],0)
