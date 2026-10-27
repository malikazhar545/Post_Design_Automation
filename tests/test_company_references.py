import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import company_references as references
import server


class CompanyReferencesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'references.json'
        self.paths = []
        for n in range(11):
            p = self.root / f'design-{n}.png'
            p.write_bytes(b'image')
            self.paths.append(str(p))
        self.patch = patch.object(server, 'reference_store', return_value=self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_company_from_title_keeps_design_in_name_and_strips_metadata(self):
        self.assertEqual(server.company_from_title('By Design Property Management 4 - Google Docs'),
                         'By Design Property Management')
        self.assertEqual(server.company_from_title('Molen_Tax_Image_Details_From_10-3.docx'), 'Molen Tax')
        self.assertEqual(server.company_from_title('Belle Clinic London UK 15'), 'Belle Clinic London UK')
        self.assertEqual(server.company_from_title('Aim Tech Design Posts 5'), 'Aim Tech')

    def test_ten_paths_persist_exact_company_and_clear_restores_automatic(self):
        automatic = self.root / 'automatic' / 'Example'
        automatic.mkdir(parents=True)
        for n in range(6):
            (automatic / f'{n}.jpg').write_bytes(b'image')
        with patch.dict(server.CFG, {'style_refs_dirs': [str(automatic.parent)], 'style_refs_max': 4}):
            original = server.style_refs('Example')
            self.assertEqual(len(original), 4)
            references.save(self.store, 'Example', self.paths[:10])
            # Pool 10 save; har call pe random style_refs_max (4) — Claude-style
            picked = server.style_refs('  EXAMPLE  ')
            self.assertEqual(len(picked), 4)
            pool = set(map(Path, self.paths[:10]))
            self.assertTrue(all(p in pool for p in picked))
            self.assertIsNone(server.custom_reference_entry('Examples'))
            self.assertEqual(len(references.load(self.store)['example']['paths']), 10)
            references.save(self.store, 'Example', [])
            self.assertEqual(server.style_refs('Example'), original)

    def test_invalid_update_preserves_saved_paths(self):
        references.save(self.store, 'Example', self.paths[:1])
        for bad in (self.paths, ['relative.png'], [str(self.root)], [str(self.root / 'missing.png')],
                    [self.paths[0], self.paths[0]], 'not a list', [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                references.save(self.store, 'Example', bad)
            self.assertEqual(references.load(self.store)['example']['paths'], self.paths[:1])

    def test_missing_custom_file_falls_back_without_failing(self):
        # deleted custom image must never stop the file (posts still get made with folder refs)
        references.save(self.store, 'Example', self.paths[:1])
        Path(self.paths[0]).unlink()
        self.assertNotIn(Path(self.paths[0]), server.style_refs('Example'))

    def test_custom_pick_is_random_four_and_stable_within_file(self):
        references.save(self.store, 'Example', self.paths[:6])
        with patch.dict(server.HUB.current, {'file': 'Example 1'}, clear=True):
            first = server.style_refs('Example')
            self.assertEqual(len(first), 4)
            self.assertEqual(server.style_refs('Example'), first)  # same file -> same set (no duplicate uploads)

    def test_alias_quotes_and_replacement_message(self):
        with patch.dict(server.CFG, {'company_aliases': {'Alias': 'Example'}}):
            result = server.dashboard_references('Alias', json.dumps(['"' + self.paths[0] + '"']))
            self.assertTrue(result['ok'])
            self.assertEqual(server.style_refs('Alias'), [Path(self.paths[0])])
            self.assertEqual(server.style_refs('Example'), [Path(self.paths[0])])
            self.assertIn('Use ONLY this reference set', server.style_reference_message('Example', '1'))

    async def test_route_requires_dashboard_header(self):
        connection = SimpleNamespace(respond=lambda status, body: SimpleNamespace(status_code=status, body=body, headers={"Content-Type": "text/plain"}))
        request = SimpleNamespace(headers={}, path='/api/action?' + urlencode({
            'do': 'save_references', 'company': 'Example', 'paths': json.dumps(self.paths[:2])}))
        response = await server.http_route(connection, request)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.store.exists())
        request.headers['X-Dashboard'] = '1'
        response = await server.http_route(connection, request)
        self.assertTrue(json.loads(response.body)['ok'])
        request.path = '/api/references'
        response = await server.http_route(connection, request)
        self.assertEqual(json.loads(response.body)['entries']['example']['paths'], self.paths[:2])


if __name__ == '__main__':
    unittest.main()
