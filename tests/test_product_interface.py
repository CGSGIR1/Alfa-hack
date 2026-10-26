"""Product UI stays separate from the operator interface and keeps reference styles."""
from pathlib import Path
from io import BytesIO
import importlib.util,json,os,sys,unittest,urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))


class ProductInterfaceTests(unittest.TestCase):
    def test_home_video_links_replace_payment_widget_and_badges(self):
        folder=ROOT/'src/integration/web_static'
        html=(folder/'index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="w-ops"',html)
        self.assertNotIn('class="demo"',html)
        self.assertNotIn('демо-остатки',html)
        self.assertIn('id="home-stories"',html)
        self.assertEqual(html.count('class="home-story"'),2)
        self.assertIn('perevodi-v-valyute-i-za-granitsu/" target="_blank" rel="noopener noreferrer"',html)
        self.assertIn('Consumer_Research_14Oct26.pdf" target="_blank" rel="noopener noreferrer"',html)
        self.assertIn('/story-cards.js',html)

    def launcher(self):
        spec=importlib.util.spec_from_file_location('product_launcher',ROOT/'tools/launch_interface.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_public_bundle_has_no_collection_dialog_or_admin_links(self):
        folder=ROOT/'src/integration/web_static'
        public='\n'.join((folder/name).read_text(encoding='utf-8') for name in ('app.js','site-adapter.js','site-panels.js'))
        for removed in ('showControls','/api/auto-poll','/api/tick','/api/generate','/manage','data-controls','integration-dialog','data-live-status'):
            self.assertNotIn(removed,public)
        self.assertIn('new EventSource',public)
        self.assertIn('class="tl"',public)
        self.assertIn('.tl li::before',(folder/'style.css').read_text(encoding='utf-8'))
        self.assertIn('background:var(--red)',(folder/'style.css').read_text(encoding='utf-8'))
        self.assertIn('margin:0;padding:0;border:0',(folder/'integration.css').read_text(encoding='utf-8'))

    def test_management_script_opens_existing_operator_page_without_new_server(self):
        launcher=self.launcher();database=ROOT/'runtime/test-only.db'
        state=json.dumps({'integration':{'database':str(database)}}).encode()
        with patch.object(launcher,'load_env'),patch.object(launcher,'load',return_value={'database':database}), \
             patch.object(launcher.urllib.request,'urlopen',return_value=BytesIO(state)), \
             patch.object(launcher,'open_browser') as opened,patch.dict(os.environ,{'APP_PORT':'9876'}):
            self.assertEqual(launcher.main(['--view','manage']),0)
            opened.assert_called_once_with('http://127.0.0.1:9876/manage')
        self.assertIn('--view manage',(ROOT/'start_management.cmd').read_text())

    def test_management_script_starts_same_backend_directly_on_operator_page(self):
        launcher=self.launcher();cfg={'database':ROOT/'runtime/test-only.db'}
        with patch.object(launcher,'load_env'),patch.object(launcher,'load',return_value=cfg), \
             patch.object(launcher.urllib.request,'urlopen',side_effect=urllib.error.URLError('not running')), \
             patch('src.integration.web.serve') as served,patch.dict(os.environ,{'APP_PORT':'9876'}):
            self.assertEqual(launcher.main(['--view','manage']),0)
            self.assertEqual(served.call_args.kwargs['browser_path'],'/manage')
            self.assertEqual(served.call_args.kwargs['port'],9876)

    def test_official_icon_is_valid_and_bundled_locally(self):
        icon=(ROOT/'src/integration/web_static/favicon.ico').read_bytes()
        self.assertEqual(icon[:4],b'\x00\x00\x01\x00')
        self.assertGreater(len(icon),100)
        self.assertIn('id="auto-toggle"',(ROOT/'src/integration/web_static/manage/index.html').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
