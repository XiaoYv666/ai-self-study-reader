import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import db
import main


class TreeOrderTests(unittest.TestCase):
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
        conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(2,'物理',1)")
        conn.execute("INSERT INTO folders(id,subject_id,parent_id,name,sort_order) VALUES(10,1,NULL,'A',0)")
        conn.execute("INSERT INTO folders(id,subject_id,parent_id,name,sort_order) VALUES(11,1,NULL,'B',1)")
        conn.execute("INSERT INTO folders(id,subject_id,parent_id,name,sort_order) VALUES(12,1,10,'子目录',0)")
        conn.execute("INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order) VALUES(20,1,NULL,'讲义.pdf','','',1,0)")
        conn.execute("INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order) VALUES(21,1,10,'习题.pdf','','',1,0)")
        conn.execute("INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order) VALUES(22,1,NULL,'例题.pdf','','',1,2)")
        conn.execute("INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order) VALUES(23,2,NULL,'力学.pdf','','',1,0)")
        conn.commit()

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        self.tmp.cleanup()

    def test_mixed_root_order_updates_both_tables_atomically(self):
        body = main.TreeOrderPatch(
            subject_id=1,
            parent_id=None,
            items=[
                main.TreeOrderItem(type="folder", id=10),
                main.TreeOrderItem(type="document", id=20),
                main.TreeOrderItem(type="document", id=22),
                main.TreeOrderItem(type="folder", id=11),
            ],
        )

        self.assertEqual(main.patch_tree_order(body), {"ok": True})

        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT sort_order FROM folders WHERE id=10").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT sort_order FROM documents WHERE id=20").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT sort_order FROM folders WHERE id=11").fetchone()[0], 3)

    def test_rejects_missing_or_foreign_container_items_without_partial_update(self):
        before = db.get_conn().execute("SELECT sort_order FROM folders WHERE id=10").fetchone()[0]
        body = main.TreeOrderPatch(
            subject_id=1,
            parent_id=None,
            items=[main.TreeOrderItem(type="folder", id=10), main.TreeOrderItem(type="document", id=21)],
        )

        with self.assertRaises(HTTPException) as ctx:
            main.patch_tree_order(body)

        self.assertEqual(ctx.exception.status_code, 422)
        detail = ctx.exception.detail
        self.assertEqual(detail["msg"], "items 必须完整且全部属于指定容器")
        self.assertEqual(detail["expected_folders"], [10, 11])
        self.assertEqual(detail["expected_docs"], [20, 22])
        self.assertEqual(detail["got_folders"], [10])
        self.assertEqual(detail["got_docs"], [21])
        self.assertEqual(detail["items"], [{"type": "folder", "id": 10}, {"type": "document", "id": 21}])
        self.assertEqual(db.get_conn().execute("SELECT sort_order FROM folders WHERE id=10").fetchone()[0], before)

    def move(self, active_type, active_id, to_subject, to_parent, containers):
        return main.patch_tree_move(main.TreeMovePatch(
            active=main.TreeOrderItem(type=active_type, id=active_id),
            to_subject_id=to_subject,
            to_parent_id=to_parent,
            containers=[main.TreeMoveContainer(**container) for container in containers],
        ))

    def test_move_reorders_mixed_folder_and_documents_in_one_container(self):
        result = self.move("document", 20, 1, None, [{
            "subject_id": 1, "parent_id": None,
            "items": [
                {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                {"type": "document", "id": 22}, {"type": "document", "id": 20},
            ],
        }])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(db.get_conn().execute("SELECT sort_order FROM documents WHERE id=20").fetchone()[0], 3)

    def test_move_document_folder_to_root_atomically(self):
        result = self.move("document", 21, 1, None, [
            {"subject_id": 1, "parent_id": 10, "items": [{"type": "folder", "id": 12}]},
            {"subject_id": 1, "parent_id": None, "items": [
                {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                {"type": "document", "id": 20}, {"type": "document", "id": 22},
                {"type": "document", "id": 21},
            ]},
        ])
        self.assertEqual(result, {"ok": True})
        self.assertIsNone(db.get_conn().execute("SELECT folder_id FROM documents WHERE id=21").fetchone()[0])

    def test_move_document_root_to_folder_atomically(self):
        result = self.move("document", 20, 1, 10, [
            {"subject_id": 1, "parent_id": None, "items": [
                {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                {"type": "document", "id": 22},
            ]},
            {"subject_id": 1, "parent_id": 10, "items": [
                {"type": "folder", "id": 12}, {"type": "document", "id": 21},
                {"type": "document", "id": 20},
            ]},
        ])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(db.get_conn().execute("SELECT folder_id FROM documents WHERE id=20").fetchone()[0], 10)

    def test_move_uses_only_final_container_after_crossing_several(self):
        # dnd-kit 可连续经过多个容器；提交只描述最终源/目标状态。
        result = self.move("document", 20, 1, 10, [
            {"subject_id": 1, "parent_id": None, "items": [
                {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                {"type": "document", "id": 22},
            ]},
            {"subject_id": 1, "parent_id": 10, "items": [
                {"type": "document", "id": 20}, {"type": "folder", "id": 12},
                {"type": "document", "id": 21},
            ]},
        ])
        self.assertEqual(result, {"ok": True})

    def test_move_reorders_folder_among_same_parent_siblings(self):
        result = self.move("folder", 11, 1, None, [{
            "subject_id": 1, "parent_id": None,
            "items": [
                {"type": "folder", "id": 11}, {"type": "folder", "id": 10},
                {"type": "document", "id": 20}, {"type": "document", "id": 22},
            ],
        }])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(db.get_conn().execute("SELECT sort_order FROM folders WHERE id=11").fetchone()[0], 0)

    def test_move_document_across_subjects_atomically(self):
        result = self.move("document", 20, 2, None, [
            {"subject_id": 1, "parent_id": None, "items": [
                {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                {"type": "document", "id": 22},
            ]},
            {"subject_id": 2, "parent_id": None, "items": [
                {"type": "document", "id": 23}, {"type": "document", "id": 20},
            ]},
        ])
        self.assertEqual(result, {"ok": True})
        row = db.get_conn().execute("SELECT subject_id,folder_id FROM documents WHERE id=20").fetchone()
        self.assertEqual(tuple(row), (2, None))

    def test_move_validation_error_rolls_back_membership_and_sort(self):
        before = tuple(db.get_conn().execute(
            "SELECT subject_id,folder_id,sort_order FROM documents WHERE id=20"
        ).fetchone())
        with self.assertRaises(HTTPException) as ctx:
            self.move("document", 20, 1, 10, [
                {"subject_id": 1, "parent_id": None, "items": [
                    {"type": "folder", "id": 10}, {"type": "folder", "id": 11},
                    {"type": "document", "id": 22},
                ]},
                {"subject_id": 1, "parent_id": 10, "items": [
                    {"type": "folder", "id": 12}, {"type": "document", "id": 20},
                    # 故意漏掉 document 21
                ]},
            ])
        self.assertEqual(ctx.exception.status_code, 422)
        after = tuple(db.get_conn().execute(
            "SELECT subject_id,folder_id,sort_order FROM documents WHERE id=20"
        ).fetchone())
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
