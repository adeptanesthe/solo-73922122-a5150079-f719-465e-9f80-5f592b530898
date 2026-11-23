"""剪枝求解器正确性测试：小随机树上全量枚举所有剪枝组合做暴力对照。

生产求解器 app/pruning.py 使用自底向上动态规划、不枚举任何剪枝组合；
本测试在深度 <=3 的小树上枚举“每个内部节点保留/收成叶子”的全部组合
（至多 2^7=128 种），逐一校验锁定约束并统计叶数与误分，证明 DP 最优解
与暴力最优解完全一致。暴力枚举只出现在测试里。
"""

import random
import time
import unittest

from app.pruning import prune_tree, validate_samples_payload
from app.tree import classify, count_leaves, validate_tree_full

CATS = ("PASS", "REWORK", "REJECT")
FEATS = ("temperature", "front", "back")


def random_tree(rng, depth=0, max_depth=3):
    if depth >= max_depth or rng.random() < 0.3:
        return {"leaf": rng.choice(CATS)}
    return {
        "feature": rng.choice(FEATS),
        "threshold": rng.randint(0, 9),
        "left": random_tree(rng, depth + 1, max_depth),
        "right": random_tree(rng, depth + 1, max_depth),
    }


def random_samples(rng, tree, count):
    """生成落在各叶子上的样品，复验类别与锁定标记随机。"""
    samples = []
    for _ in range(count):
        reading = {f: rng.randint(0, 9) for f in FEATS}
        samples.append({
            **reading,
            "category": rng.choice(CATS),
            "locked": rng.random() < 0.4,
        })
    return samples


# ------------------------------------------------ 暴力枚举（仅测试用）
def all_prunings(node, allowed_labels):
    """枚举一棵子树的全部合法剪枝（结构 + 可选叶子类别）。

    返回 [(pruned_json, leaf_count), ...]；allowed_labels 为该子树原有叶子
    类别集合，收成叶子时类别只能从中选择。
    """
    if "leaf" in node:
        return [({"leaf": node["leaf"]}, 1)]

    left_labels = _subtree_labels(node["left"])
    right_labels = _subtree_labels(node["right"])
    left_options = all_prunings(node["left"], left_labels)
    right_options = all_prunings(node["right"], right_labels)

    results = []
    # 选项 A：整棵子树收成一个叶子，类别取自原子树叶子类别。
    for c in sorted(allowed_labels):
        results.append(({"leaf": c}, 1))
    # 选项 B：保留原分支，左右剪枝组合笛卡尔积。
    for l_tree, l_k in left_options:
        for r_tree, r_k in right_options:
            results.append(({
                "feature": node["feature"],
                "threshold": node["threshold"],
                "left": l_tree,
                "right": r_tree,
            }, l_k + r_k))
    return results


def _subtree_labels(node):
    if "leaf" in node:
        return {node["leaf"]}
    return _subtree_labels(node["left"]) | _subtree_labels(node["right"])


def brute_force_prune(tree, max_leaves, samples):
    """全枚举暴力解，返回 (status, leaves_after, mis_after)。"""
    labels = _subtree_labels(tree)
    candidates = all_prunings(tree, labels)
    readings = [
        {"temperature": s["temperature"], "front": s["front"], "back": s["back"]}
        for s in samples
    ]
    preds_before = [classify(tree, r) for r in readings]

    feasible = []
    for pruned, leaf_count in candidates:
        if leaf_count > max_leaves:
            continue
        preds_after = [classify(pruned, r) for r in readings]
        # 锁定样品预测必须与剪枝前相同。
        if any(s["locked"] and a != b
               for s, a, b in zip(samples, preds_before, preds_after)):
            continue
        mis = sum(1 for s, p in zip(samples, preds_after) if s["category"] != p)
        feasible.append((mis, leaf_count))
    if not feasible:
        return "no_solution", None, None
    # 误分最少，平局叶数最少。
    mis, leaf_count = min(feasible, key=lambda item: (item[0], item[1]))
    return "solved", leaf_count, mis


class PruningRandomTests(unittest.TestCase):
    def test_dp_matches_brute_force_on_random_small_trees(self):
        rng = random.Random(20261123)
        checked = 0
        for _ in range(300):
            tree = random_tree(rng, max_depth=3)
            validate_tree_full(tree)
            sample_count = rng.randint(1, 12)
            samples = random_samples(rng, tree, sample_count)
            max_leaves = rng.randint(1, max(1, count_leaves(tree)))

            result = prune_tree(tree, max_leaves, samples)
            exp_status, exp_leaves, exp_mis = brute_force_prune(
                tree, max_leaves, samples,
            )
            self.assertEqual(result["status"], exp_status,
                             msg=f"status mismatch max_leaves={max_leaves}")
            if exp_status == "solved":
                self.assertEqual(result["leaves_after"], exp_leaves)
                self.assertEqual(result["misclass_after"], exp_mis)
                self.assertLessEqual(result["leaves_after"], max_leaves)
                # 锁定样品零违约是硬约束。
                self.assertEqual(result["locked_violations"], 0)
            checked += 1
        self.assertGreaterEqual(checked, 300)

    def test_locked_predictions_never_change(self):
        rng = random.Random(7)
        for _ in range(100):
            tree = random_tree(rng, max_depth=3)
            samples = random_samples(rng, tree, 10)
            if not any(s["locked"] for s in samples):
                samples[0]["locked"] = True
            result = prune_tree(tree, count_leaves(tree), samples)
            self.assertEqual(result["status"], "solved")
            readings = [
                {"temperature": s["temperature"], "front": s["front"],
                 "back": s["back"]}
                for s in samples
            ]
            before = [classify(tree, r) for r in readings]
            after = [classify(result["pruned_tree"], r) for r in readings]
            for s, a, b in zip(samples, before, after):
                if s["locked"]:
                    self.assertEqual(a, b)

    def test_no_solution_when_lock_forces_more_leaves_than_cap(self):
        # 根分两支，两条锁定样品预测不同 → 根不能收成 1 叶，上限 1 必无解。
        tree = {
            "feature": "temperature", "threshold": 5,
            "left": {"leaf": "A"}, "right": {"leaf": "B"},
        }
        samples = [
            {"temperature": 1, "front": 0, "back": 0, "category": "A", "locked": True},
            {"temperature": 9, "front": 0, "back": 0, "category": "B", "locked": True},
        ]
        result = prune_tree(tree, 1, samples)
        self.assertEqual(result["status"], "no_solution")
        self.assertIsNone(result["pruned_tree"])
        # 上限放宽到 2 即有解（保留原树）。
        result2 = prune_tree(tree, 2, samples)
        self.assertEqual(result2["status"], "solved")
        self.assertEqual(result2["leaves_after"], 2)

    def test_tie_prefers_fewer_leaves(self):
        # 单叶子树恒为 1 叶；构造一个“收枝与保留误分相同”的场景：
        # 两叶同类别 A、样品全为 A，收成 1 叶与保留 2 叶误分都为 0，
        # 平局规则要求选择更少的 1 叶。
        tree = {
            "feature": "temperature", "threshold": 5,
            "left": {"leaf": "A"}, "right": {"leaf": "A"},
        }
        samples = [
            {"temperature": 1, "front": 0, "back": 0, "category": "A", "locked": False},
            {"temperature": 9, "front": 0, "back": 0, "category": "A", "locked": False},
        ]
        result = prune_tree(tree, 8, samples)
        self.assertEqual(result["status"], "solved")
        self.assertEqual(result["leaves_after"], 1)
        self.assertEqual(result["misclass_after"], 0)
        self.assertEqual(result["pruned_tree"], {"leaf": "A"})

    def test_collapsed_label_must_come_from_original_subtree(self):
        # 子树原有类别只有 A/B，任何剪枝结果都不得出现新类别 C。
        tree = {
            "feature": "temperature", "threshold": 5,
            "left": {"leaf": "A"},
            "right": {
                "feature": "front", "threshold": 4,
                "left": {"leaf": "B"}, "right": {"leaf": "A"},
            },
        }
        samples = [
            {"temperature": t, "front": f, "back": 0,
             "category": "A", "locked": False}
            for t in (1, 9) for f in (1, 9)
        ]
        result = prune_tree(tree, 4, samples)
        self.assertEqual(result["status"], "solved")

        def labels(node):
            if "leaf" in node:
                return {node["leaf"]}
            return labels(node["left"]) | labels(node["right"])

        self.assertTrue(labels(result["pruned_tree"]).issubset({"A", "B"}))

    def test_large_tree_performance_without_enumeration(self):
        # 128 叶满树 + 500 条样品，DP 必须在毫秒~百毫秒级完成（非穷举）。
        def full(depth, path):
            if depth == 7:
                return {"leaf": f"L{path:07b}"}
            return {
                "feature": FEATS[depth % 3],
                "threshold": (0, 65535, 32767)[depth % 3],
                "left": full(depth + 1, path << 1),
                "right": full(depth + 1, (path << 1) | 1),
            }

        tree = full(0, 0)
        self.assertEqual(count_leaves(tree), 128)
        rng = random.Random(11)
        samples = [{
            "temperature": rng.randint(0, 65535),
            "front": rng.randint(0, 65535),
            "back": rng.randint(0, 65535),
            "category": rng.choice(CATS),
            "locked": (i % 5 == 0),
        } for i in range(500)]
        start = time.perf_counter()
        result = prune_tree(tree, 32, samples)
        elapsed = time.perf_counter() - start
        self.assertEqual(result["status"], "solved")
        self.assertLessEqual(result["leaves_after"], 32)
        self.assertEqual(result["locked_violations"], 0)
        self.assertLess(elapsed, 3.0)


class PruningValidationTests(unittest.TestCase):
    def _payload(self, **over):
        base = {
            "max_leaves": 4,
            "samples": [{
                "temperature": 1, "front": 2, "back": 3,
                "category": "PASS", "locked": False,
            }],
        }
        base.update(over)
        return base

    def test_valid_payload(self):
        ml, samples = validate_samples_payload(self._payload())
        self.assertEqual(ml, 4)
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0]["locked"])

    def test_max_leaves_bounds_and_type(self):
        for bad in (0, 129, -1, True, "4", 1.5):
            with self.assertRaises(ValueError):
                validate_samples_payload(self._payload(max_leaves=bad))

    def test_sample_count_bounds(self):
        with self.assertRaises(ValueError):
            validate_samples_payload(self._payload(samples=[]))
        with self.assertRaises(ValueError):
            validate_samples_payload(self._payload(
                samples=[self._payload()["samples"][0] for _ in range(501)]))
        # 500 条合法。
        ml, samples = validate_samples_payload(self._payload(
            samples=[self._payload()["samples"][0] for _ in range(500)]))
        self.assertEqual(len(samples), 500)

    def test_sample_field_types_and_ranges(self):
        good = self._payload()["samples"][0]
        for key, bad in (
            ("temperature", 65536), ("front", -1), ("back", 1.0),
            ("temperature", True), ("category", ""), ("category", 123),
            ("locked", "true"),
        ):
            broken = {**good, key: bad}
            with self.assertRaises(ValueError, msg=f"{key}={bad!r} 应被拒绝"):
                validate_samples_payload(self._payload(samples=[broken]))

    def test_missing_and_extra_fields(self):
        with self.assertRaises(ValueError):
            validate_samples_payload({"max_leaves": 2})
        with self.assertRaises(ValueError):
            payload = self._payload()
            payload["samples"][0]["extra"] = 1
            validate_samples_payload(payload)


if __name__ == "__main__":
    unittest.main()
