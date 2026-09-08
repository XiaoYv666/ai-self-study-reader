import json
import tempfile
import unittest
from pathlib import Path

import db
import main


class ConversationImageHistoryTests(unittest.TestCase):
    def setUp(self):
        old = getattr(db._local, "conn", None)
        if old is not None:
            old.close()
            del db._local.conn
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.DATA_DIR = Path(self.tmp.name)
        db.init_db()
        conn = db.get_conn()
        conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(1,'数学',0)")
        conn.execute(
            "INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order)"
            " VALUES(1,1,NULL,'讲义.pdf','','',1,0)"
        )
        conn.commit()

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        self.tmp.cleanup()

    def test_save_and_reload_preserves_generated_image_metadata(self):
        image = {
            "url": "/api/images/0123456789abcdef0123456789abcdef.png",
            "prompt": "plot y=x^2",
            "source": "plot",
            "degraded": False,
        }
        body = main.ConvIn(
            doc_id=1,
            pages=[1],
            messages=[
                {"role": "user", "content": "画出抛物线"},
                {"role": "assistant", "content": "如下图。", "images": [image]},
            ],
        )

        conv_id = main.save_conversation(body)["conv_id"]
        row = db.get_conn().execute(
            "SELECT messages FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        self.assertEqual(json.loads(row["messages"])[1]["images"], [image])

        saved = main.list_conversations(doc_id=1, page=1)
        self.assertEqual(saved[0]["messages"][1]["images"], [image])

    def test_old_text_only_conversation_remains_readable(self):
        db.get_conn().execute(
            "INSERT INTO conversations(doc_id,pages,messages) VALUES(?,?,?)",
            (1, "[1]", '[{"role":"assistant","content":"旧文字回答"}]'),
        )
        db.get_conn().commit()

        saved = main.list_conversations(doc_id=1, page=1)
        self.assertEqual(saved[0]["messages"], [{"role": "assistant", "content": "旧文字回答"}])


if __name__ == "__main__":
    unittest.main()
