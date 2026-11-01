import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import seriamo_diagnostics as diag


class DiagnosticsTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(diag.requests, 'post') as post:
            self.assertIsNone(diag.service_start())
            post.assert_not_called()

    def test_envelope_shape(self):
        env = diag.build_envelope('calendar_sync', {'outcome': 'ok'}, now=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(env['schema_version'], 1)
        (event,) = env['events']
        self.assertEqual(set(event), {'event_id', 'product', 'event', 'app_version', 'platform', 'occurred_at', 'props'})
        self.assertEqual(event['product'], 'pronto')
        self.assertEqual(event['platform'], 'linux')
        self.assertEqual(event['occurred_at'], '2026-11-01T08:00:00Z')
        self.assertEqual(event['props'], {'outcome': 'ok'})
        self.assertRegex(event['app_version'], r'^[0-9A-Za-z.+-]{1,32}$')
        self.assertRegex(event['event_id'], r'^[A-Za-z0-9-]{8,64}$')
        self.assertNotIn('install_id', event)

    def test_sends_when_enabled(self):
        with mock.patch.dict(os.environ, {'SERIAMO_DIAGNOSTICS': '1', 'SERIAMO_TELEMETRY_URL': 'https://example.test/v1/events'}), \
                mock.patch.object(diag.requests, 'post') as post:
            diag.calendar_sync(False).join(2)
            url = post.call_args.args[0]
            body = post.call_args.kwargs['json']
            self.assertEqual(url, 'https://example.test/v1/events')
            self.assertEqual(body['events'][0]['event'], 'calendar_sync')
            self.assertEqual(body['events'][0]['props'], {'outcome': 'error'})

    def test_network_errors_are_swallowed(self):
        with mock.patch.dict(os.environ, {'SERIAMO_DIAGNOSTICS': 'true'}), \
                mock.patch.object(diag.requests, 'post', side_effect=diag.requests.ConnectionError('down')):
            diag.record('service_start', sync=True)

    def test_service_start_has_no_props(self):
        env = diag.build_envelope('service_start')
        self.assertNotIn('props', env['events'][0])


if __name__ == '__main__':
    unittest.main()
