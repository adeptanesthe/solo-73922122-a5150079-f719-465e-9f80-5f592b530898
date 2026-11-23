"""线程安全的 JSON 文件持久化与试算任务管理。

状态文件结构：
{
  "revision": 整数,                 # 每次保存原树/草稿/确认后自增
  "original_tree": 树 JSON | null,
  "candidate_tree": 树 JSON | null,
  "draft": 工况草稿 | null,
  "confirmation": {                 # 已确认的接管方案，null 表示尚无确认
      "revision": 确认时的草稿版本号,
      "zone": [lo, hi],
      "draft": {…工况…},
      "candidate_tree": {…确认时快照…},
      "trial_id": 试算任务 id,
      "confirmed_at": ISO 时间
  } | null
}

试算/剪枝任务保存在进程内存中（守护线程异步执行），重启后需重新发起；
草稿与确认结果落盘，重启不丢。
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class Store:
    MAX_JOBS = 20

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._jobs_lock = threading.RLock()
        self._jobs = {}  # job_id -> 试算任务 dict
        self._prune_jobs = {}  # job_id -> 剪枝任务 dict
        # 测试钩子：pruning_gate 若设置，剪枝 worker 进入计算前会等待它；
        # worker 真正到达等待点后置位 pruning_entered，供测试做双向同步。
        # 生产环境两者均为 None，零开销。
        self.pruning_gate = None
        self.pruning_entered = None
        # 登记剪枝 worker 线程，便于优雅关闭/测试等待，避免线程跨请求残留。
        self._prune_threads = []
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._lock:
            if not os.path.exists(path):
                self._write_locked({
                    "revision": 0,
                    "original_tree": None,
                    "candidate_tree": None,
                    "draft": None,
                    "confirmation": None,
                })

    # ---------- 底层读写 ----------
    def _read_locked(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write_locked(self, state):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get_state(self):
        with self._lock:
            state = self._read_locked()
            revision = state["revision"]
        state["jobs"] = self.public_jobs(revision)
        state["pruning_jobs"] = self.public_pruning_jobs(revision)
        return state

    def get_revision(self):
        with self._lock:
            return self._read_locked()["revision"]

    # ---------- 保存 ----------
    def save_original_tree(self, tree):
        with self._lock:
            state = self._read_locked()
            state["original_tree"] = tree
            state["revision"] += 1
            self._write_locked(state)
            return state["revision"]

    def save_draft(self, candidate_tree, draft):
        """保存候选树与工况草稿（可在试算/剪枝进行中随时调用）。"""
        with self._lock:
            state = self._read_locked()
            state["candidate_tree"] = candidate_tree
            state["draft"] = draft
            state["revision"] += 1
            self._write_locked(state)
            return state["revision"]

    def apply_pruned_tree(self, job):
        """剪枝任务完成后尝试用结果替换草稿里的候选树。

        仅当【任务发起时的 revision 仍是当前 revision】才替换；
        期间草稿被保存过（哪怕改回原值，revision 也已自增）则拒绝替换，
        保留最新草稿。返回 (applied, current_revision, reason)。
        替换属于一次草稿修改，revision 同样自增，使旧温区试算立即过期。
        """
        with self._lock:
            state = self._read_locked()
            current = state["revision"]
            if job["revision"] != current:
                return False, current, (
                    f"候选树草稿在剪枝期间已被修改（任务基于 revision={job['revision']}，"
                    f"当前为 revision={current}），剪枝结果不替换草稿，最新草稿已保留"
                )
            state["candidate_tree"] = json.loads(
                json.dumps(job["result"]["pruned_tree"])
            )
            state["revision"] += 1
            self._write_locked(state)
            return True, state["revision"], "剪枝结果已替换草稿中的候选树"

    def confirm(self, job):
        """依据一个成功且未过期的试算任务写入确认。"""
        with self._lock:
            state = self._read_locked()
            state["confirmation"] = {
                "revision": job["revision"],
                "zone": list(job["result"]["zone"]),
                "draft": json.loads(json.dumps(job["snapshot"]["draft"])),
                "candidate_tree": json.loads(
                    json.dumps(job["snapshot"]["candidate_tree"])
                ),
                "trial_id": job["id"],
                "confirmed_at": _now_iso(),
            }
            state["revision"] += 1
            self._write_locked(state)
            return state["revision"], state["confirmation"]

    # ---------- 试算任务（内存态）----------
    def create_job(self, snapshot, revision):
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "pending",  # pending | running | solved | no_solution | error
            "revision": revision,
            "snapshot": snapshot,
            "result": None,
            "error": None,
            "created_at": _now_iso(),
            "finished_at": None,
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
            # 仅保留最近 MAX_JOBS 个任务，避免无界增长。
            if len(self._jobs) > self.MAX_JOBS:
                oldest = sorted(self._jobs, key=lambda jid: self._jobs[jid]["created_at"])
                for old_id in oldest[: len(self._jobs) - self.MAX_JOBS]:
                    self._jobs.pop(old_id, None)
        return job

    def update_job(self, job_id, **changes):
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.update(changes)
            return job

    def get_job(self, job_id):
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def public_jobs(self, current_revision=None):
        """对外任务视图；current_revision 给定时标注 stale（草稿已改过）。"""
        if current_revision is None:
            current_revision = self.get_revision()
        with self._jobs_lock:
            return [{
                "id": job["id"],
                "status": job["status"],
                "revision": job["revision"],
                "stale": job["revision"] != current_revision,
                "result": job["result"],
                "error": job["error"],
                "created_at": job["created_at"],
                "finished_at": job["finished_at"],
            } for job in self._jobs.values()]

    # ---------- 剪枝任务（内存态）----------
    def create_pruning_job(self, snapshot, revision, max_leaves, samples):
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            # pending | running | solved | no_solution | error
            "status": "pending",
            "revision": revision,
            "snapshot": snapshot,
            "max_leaves": max_leaves,
            "samples": samples,
            "result": None,
            "error": None,
            # applied：成功且版号有效并已替换草稿；superseded：版号变化未替换。
            "applied": False,
            "apply_revision": None,
            "apply_reason": None,
            "created_at": _now_iso(),
            "finished_at": None,
        }
        with self._jobs_lock:
            self._prune_jobs[job_id] = job
            if len(self._prune_jobs) > self.MAX_JOBS:
                oldest = sorted(
                    self._prune_jobs,
                    key=lambda jid: self._prune_jobs[jid]["created_at"],
                )
                for old_id in oldest[: len(self._prune_jobs) - self.MAX_JOBS]:
                    self._prune_jobs.pop(old_id, None)
        return job

    def update_pruning_job(self, job_id, **changes):
        with self._jobs_lock:
            job = self._prune_jobs.get(job_id)
            if job is None:
                return None
            job.update(changes)
            return job

    def get_pruning_job(self, job_id):
        with self._jobs_lock:
            return self._prune_jobs.get(job_id)

    def register_pruning_thread(self, thread):
        with self._jobs_lock:
            # 保留“尚未 start”或“仍存活”的线程（登记发生在 start 之前），
            # 仅清理已经结束的线程，避免列表无界增长。
            self._prune_threads = [
                t for t in self._prune_threads
                if t.ident is None or t.is_alive()
            ]
            self._prune_threads.append(thread)

    def join_pruning_threads(self, timeout=5):
        """等待全部剪枝 worker 结束（主要用于优雅关闭与测试同步）。"""
        with self._jobs_lock:
            threads = list(self._prune_threads)
        for thread in threads:
            thread.join(timeout=timeout)

    def finish_pruning_job(self, job_id, result):
        """剪枝计算成功后的原子收尾：写结果，并在版号仍有效时替换候选树。

        把“更新任务状态”和“应用/拒绝替换”合并到同一把任务锁内，避免页面
        轮询读到“已 solved 但尚无 apply_reason”的中间态。返回更新后的 job。
        """
        with self._jobs_lock:
            job = self._prune_jobs.get(job_id)
            if job is None:
                return None
            job["status"] = result["status"]
            job["result"] = result
            job["finished_at"] = _now_iso()
            if result["status"] != "solved":
                job["applied"] = False
                job["apply_reason"] = "剪枝未成功（无解或计算失败），草稿保持不变"
                return job
            # 在任务锁内调用状态替换（apply_pruned_tree 内部另取状态锁，
            # 两把锁均为 RLock 且此处不存在反向获取顺序，无死锁）。
            applied, current_revision, reason = self.apply_pruned_tree(job)
            job["applied"] = applied
            job["apply_revision"] = current_revision if applied else None
            job["apply_reason"] = reason
            return job

    def fail_pruning_job(self, job_id, error):
        """计算失败的原子收尾：记录错误并标明草稿不变。"""
        with self._jobs_lock:
            job = self._prune_jobs.get(job_id)
            if job is None:
                return None
            job["status"] = "error"
            job["error"] = error
            job["finished_at"] = _now_iso()
            job["applied"] = False
            job["apply_reason"] = f"剪枝计算失败：{error}，草稿保持不变"
            return job

    def public_pruning_jobs(self, current_revision=None):
        """对外剪枝任务视图，含过期标记与替换结果。"""
        if current_revision is None:
            current_revision = self.get_revision()
        with self._jobs_lock:
            return [{
                "id": job["id"],
                "status": job["status"],
                "revision": job["revision"],
                "stale": job["revision"] != current_revision,
                "current_revision": current_revision,
                "max_leaves": job["max_leaves"],
                "sample_count": len(job["samples"]),
                "result": job["result"],
                "error": job["error"],
                "applied": job["applied"],
                "superseded": (
                    job["status"] == "solved" and not job["applied"]
                    and job["finished_at"] is not None
                ),
                "apply_revision": job["apply_revision"],
                "apply_reason": job["apply_reason"],
                "created_at": job["created_at"],
                "finished_at": job["finished_at"],
            } for job in self._prune_jobs.values()]
