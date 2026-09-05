"""Regression checks; all store mutations use TemporaryDirectory, never data/."""
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import serve
from tools.make_guide_course import mp3_frames


class GuideTests(unittest.TestCase):
    def test_package_sources_images_and_audio(self):
        with zipfile.ZipFile(ROOT/'demo'/'教你使用lecturelite.lecture.zip') as z:
            meta=json.loads(z.read('lecture.json'))
            self.assertEqual(len(meta['files']),9)
            images=set()
            for f in meta['files']:
                md=z.read(f['name']).decode('utf-8')
                for ref in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',md):
                    self.assertIn(ref,z.namelist())
                    self.assertTrue(z.read(ref).startswith((b'\x89PNG',b'\xff\xd8\xff')))
                    images.add(ref)
            self.assertGreaterEqual(len(images),10)
            _,duration=mp3_frames(z.read('audio.mp3'))
            self.assertAlmostEqual(duration/1000,meta['duration'],delta=.02)
            self.assertEqual([s['fi'] for s in meta['fileSwitches']],list(range(9)))
            for key in ['states','script','fileSwitches']:
                self.assertEqual(meta[key],sorted(meta[key],key=lambda s:s['t']))
                self.assertLessEqual(meta[key][-1]['t'],duration)

    def test_seed_preserves_users_favorites_and_retired_demo(self):
        with tempfile.TemporaryDirectory(prefix='lecturelite-unit-') as tmp:
            root=Path(tmp)
            db={key:{} for key in serve._db}
            db['users']['alice']={'test':'unchanged'}
            db['recordings']['old']={'demo':True,'stored':'old.zip'}
            db['recordings']['mine']={'owner':'alice','title':'Keep me','stored':'mine.zip'}
            db['user_favorites']['alice']={'mine':123}
            shared=root/'shared';shared.mkdir()
            (shared/'old.zip').write_bytes(b'preserved')
            with patch.multiple(serve,_db=db,DATA_DIR=root,DB_FILE=root/'store.json',SHARED_DIR=shared):
                serve._seed_demo_courses()
                serve._seed_demo_courses()
                serve._purge_expired_trash()
                self.assertEqual([rid for rid,r in db['recordings'].items() if r.get('demo') and not r.get('deleted')],['dmguide'])
                self.assertEqual(db['users']['alice'],{'test':'unchanged'})
                self.assertEqual(db['user_favorites']['alice'],{'mine':123})
                self.assertEqual(db['recordings']['mine']['title'],'Keep me')
                self.assertTrue((shared/'old.zip').exists())
                self.assertTrue(serve._can_view('alice','dmguide'))
                self.assertFalse(serve._can_view('alice','old'))

    def test_own_favorite_is_in_both_lists(self):
        db={key:{} for key in serve._db}
        db['recordings']['mine']={'id':'mine','owner':'alice','title':'Mine'}
        db['user_favorites']['alice']={'mine':1}
        replies=[]
        handler=SimpleNamespace(_session_uid=lambda:'alice',_rec_item=serve.Handler._rec_item,_send_json=lambda code,data:replies.append((code,data)))
        with patch.object(serve,'_db',db):
            serve.Handler._handle_recordings_list(handler)
        self.assertEqual(replies[0][0],200)
        self.assertEqual(len(replies[0][1]['mine']),1)
        self.assertEqual(len(replies[0][1]['favorites']),1)

if __name__=='__main__':
    unittest.main()
