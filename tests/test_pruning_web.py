"""剪枝 Web 端到端自测：发起、自动替换候选树、版号变化保留草稿、无解、校验。"""

import os
import tempfile
import threading
import time
import unittest

from app.examples import PRUNING_EXAMPLES
from app.tree import count_leaves
from app.web import create_app


class PruningWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.json")
        app = create_app(data_file=self.db)
        app.testing = True
        self.client = app.test_client()
        self.ok_example = next(
            e for e in PRUNING_EXAMPLES if e["key"] == "prune_ok"
        )
        self.no_solution_example = next(
            e for e in PRUNING_EXAMPLES if e["key"] == "prune_locked_no_solution"
        )
        # 先保存候选树（剪枝的前置条件）。
        r = self.client.post("/api/draft", json={
            "candidate_tree": self.ok_example["candidate_tree"],
            "draft": {
                "temperature": [0, 65535], "front": [0, 65535],
                "back": [0, 65535], "diff": [-65535, 65535],
            },
        })
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        # 等待本应用的剪枝 worker 全部结束，避免 daemon 线程跨测试残留。
        self.client.application.config["STORE"].join_pruning_threads()
        self.tmp.cleanup()

    def _start_pruning(self, max_leaves=None, samples=None):
        payload = {
            "max_leaves": self.ok_example["max_leaves"]
            if max_leaves is None else max_leaves,
            "samples": self.ok_example["samples"] if samples is None else samples,
        }
        r = self.client.post("/api/pruning", json=payload)
        self.assertEqual(r.status_code, 201, msg=r.get_data(as_text=True))
        return r.get_json()["job_id"]

    def _wait_pruning(self, job_id):
        # 先等 worker 线程彻底结束（保证 finish_pruning_job 的所有字段都已
        # 落盘），再读取最终任务视图，避免“状态已 solved 但 apply_reason
        # 尚未写”的轮询竞态。
        store = self.client.application.config["STORE"]
        store.join_pruning_threads(timeout=5)
        for _ in range(60):
            job = self.client.get(f"/api/pruning/{job_id}").get_json()["job"]
            if job["status"] not in ("pending", "running"):
                return job
            time.sleep(0.05)
        self.fail("剪枝任务超时未完成")

    # ---------- 成功并自动替换 ----------
    def test_successful_pruning_replaces_candidate_tree(self):
        before = self.client.get("/api/state").get_json()
        self.assertEqual(count_leaves(before["candidate_tree"]), 8)
        revision_before = before["revision"]

        job_id = self._start_pruning()
        job = self._wait_pruning(job_id)
        self.assertEqual(job["status"], "solved")
        self.assertTrue(job["applied"])
        self.assertEqual(job["result"]["leaves_before"], 8)
        self.assertEqual(job["result"]["leaves_after"], 2)
        self.assertEqual(job["result"]["misclass_before"], 7)
        self.assertEqual(job["result"]["misclass_after"], 0)
        self.assertEqual(job["result"]["locked_violations"], 0)

        state = self.client.get("/api/state").get_json()
        # 候选树已被替换为 2 叶剪枝结果，且 revision 自增。
        self.assertEqual(count_leaves(state["candidate_tree"]), 2)
        self.assertEqual(state["revision"], revision_before + 1)
        # 工况草稿不受影响。
        self.assertIsNotNone(state["draft"])

    def test_applied_tree_preserves_locked_predictions(self):
        job_id = self._start_pruning()
        job = self._wait_pruning(job_id)
        self.assertEqual(job["status"], "solved")
        state = self.client.get("/api/state").get_json()
        # 用 /api/classify 间接验证替换后的候选树对锁定样品预测不变。
        # 这里直接对剪枝前后树做锁定样品路由比对。
        from app.tree import classify
        original = self.ok_example["candidate_tree"]
        for sample in self.ok_example["samples"]:
            if not sample["locked"]:
                continue
            reading = {k: sample[k] for k in ("temperature", "front", "back")}
            self.assertEqual(
                classify(original, reading),
                classify(state["candidate_tree"], reading),
            )

    # ---------- 版号变化：保留最新草稿 ----------
    def test_revision_change_keeps_latest_draft(self):
        # 用 Store 上的测试钩子（双向握手事件）把剪枝计算阻塞到“改稿保存”
        # 之后，确定性地复现剪枝期间改稿，避免毫秒级完成造成时序竞态。
        store = self.client.application.config["STORE"]
        gate = threading.Event()
        entered = threading.Event()
        store.pruning_gate = gate
        store.pruning_entered = entered
        try:
            job_id = self.client.post("/api/pruning", json={
                "max_leaves": self.ok_example["max_leaves"],
                "samples": self.ok_example["samples"],
            }).get_json()["job_id"]

            # 等 worker 真正到达 gate 等待点，再保存草稿使版号自增。
            self.assertTrue(entered.wait(timeout=5), "剪枝 worker 未到达同步点")
            state0 = self.client.get("/api/state").get_json()
            saved = self.client.post("/api/draft", json={
                "candidate_tree": state0["candidate_tree"],
                "draft": state0["draft"],
            })
            self.assertEqual(saved.status_code, 200)
            revision_after_save = saved.get_json()["revision"]
            gate.set()

            job = self._wait_pruning(job_id)
        finally:
            store.pruning_gate = None
            store.pruning_entered = None

        self.assertEqual(job["status"], "solved")
        # 版号已变化：不能替换草稿。
        self.assertFalse(job["applied"])
        self.assertTrue(job["stale"])
        self.assertIn("revision", job["apply_reason"])

        state = self.client.get("/api/state").get_json()
        # 候选树仍是 8 叶的最新草稿，未被剪枝结果覆盖。
        self.assertEqual(count_leaves(state["candidate_tree"]), 8)
        self.assertEqual(state["revision"], revision_after_save)

    def test_saving_draft_during_pruning_never_blocked(self):
        job_id = self._start_pruning()
        state = self.client.get("/api/state").get_json()
        r = self.client.post("/api/draft", json={
            "candidate_tree": state["candidate_tree"], "draft": state["draft"],
        })
        self.assertEqual(r.status_code, 200)
        job = self._wait_pruning(job_id)
        self.assertIn(job["status"], ("solved",))

    # ---------- 无解：保留草稿 ----------
    def test_no_solution_keeps_draft_and_explains(self):
        before = self.client.get("/api/state").get_json()
        job_id = self._start_pruning(
            max_leaves=self.no_solution_example["max_leaves"],
            samples=self.no_solution_example["samples"],
        )
        job = self._wait_pruning(job_id)
        self.assertEqual(job["status"], "no_solution")
        self.assertFalse(job["applied"])
        self.assertIn("无解", job["result"]["reason"])
        state = self.client.get("/api/state").get_json()
        # 草稿与版号均不变。
        self.assertEqual(state["revision"], before["revision"])
        self.assertEqual(count_leaves(state["candidate_tree"]), 8)

    # ---------- 校验 ----------
    def test_validation_errors(self):
        good_sample = self.ok_example["samples"][0]
        # 叶数上限越界
        r = self.client.post("/api/pruning", json={
            "max_leaves": 0, "samples": [good_sample]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/pruning", json={
            "max_leaves": 129, "samples": [good_sample]})
        self.assertEqual(r.status_code, 400)
        # 布尔冒充叶数上限
        r = self.client.post("/api/pruning", json={
            "max_leaves": True, "samples": [good_sample]})
        self.assertEqual(r.status_code, 400)
        # 样品为空 / 超 500
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4, "samples": []})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4, "samples": [good_sample] * 501})
        self.assertEqual(r.status_code, 400)
        # 样品字段非法：locked 非布尔、温度越界、类别为空、缺字段
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4,
            "samples": [{**good_sample, "locked": "yes"}]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4,
            "samples": [{**good_sample, "temperature": 70000}]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4,
            "samples": [{**good_sample, "category": "  "}]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/pruning", json={
            "max_leaves": 4,
            "samples": [{"temperature": 1, "front": 2, "back": 3}]})
        self.assertEqual(r.status_code, 400)

    def test_pruning_without_candidate_tree_rejected(self):
        db2 = os.path.join(self.tmp.name, "empty.json")
        client2 = create_app(data_file=db2).test_client()
        r = client2.post("/api/pruning", json={
            "max_leaves": 2,
            "samples": [{"temperature": 1, "front": 2, "back": 3,
                         "category": "A", "locked": False}],
        })
        self.assertEqual(r.status_code, 409)

    def test_unknown_pruning_job_404(self):
        r = self.client.get("/api/pruning/nonexistent")
        self.assertEqual(r.status_code, 404)

    def test_pruning_applied_survives_app_recreation(self):
        job_id = self._start_pruning()
        self._wait_pruning(job_id)
        # 重建应用模拟重启：替换后的候选树已落盘。
        app2 = create_app(data_file=self.db)
        client2 = app2.test_client()
        state = client2.get("/api/state").get_json()
        self.assertEqual(count_leaves(state["candidate_tree"]), 2)
        # 内存任务重启清空。
        self.assertEqual(state["pruning_jobs"], [])

    def test_pruning_examples_endpoint(self):
        data = self.client.get("/api/pruning-examples").get_json()
        keys = [e["key"] for e in data["examples"]]
        self.assertIn("prune_ok", keys)
        self.assertIn("prune_locked_no_solution", keys)


if __name__ == "__main__":
    unittest.main()
