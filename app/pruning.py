"""候选决策树剪枝求解器（动态规划，不穷举任何剪枝组合）。

问题定义
--------
对候选决策树做“最小复杂度代价复杂度剪枝”：
  - 每个分支节点只有两种处置：**保留原分支**，或把**整棵子树收成一个叶子**；
  - 收成叶子时，叶子类别只能从该子树原有的叶子类别中选择；
  - 锁定样品（locked=true）的预测类别必须与剪枝前完全相同；
  - 在叶数不超过上限 max_leaves 的所有可行剪枝中，取误分样品数最少的；
    误分数相同取叶数更少的。
  - 样品 1..500 条，叶数上限 1..128。

为什么不是指数级
----------------
若枚举“每个内部节点剪/不剪”的组合，方案数随内部节点数指数增长（最多 127
个内部节点，完全不可行）。本模块在树节点上做自底向上的动态规划：

    dp[u][k][c] = 以 u 为根的子树被剪成恰好 k 个叶子、且根处置对应类别/状态 c
                  时，子树内最小的误分样品数（含回溯指针）

  - 叶子节点：只能原样保留（单叶子树“收成叶子”的可选类别只有它自己），
    故 dp 中 k=1 恒有解；
  - 内部节点：
      * 收成叶子：k=1，类别 c 取自该子树原有叶子类别，且必须让所有经过 u 的
        锁定样品的预测保持不变，否则该类别不可行；
      * 保留分支：k=k1+k2，代价为左右子树最优解之和，对左右叶数做一次
        O(k²) 卷积；
  - 同一 (k, c) 只保留最小误分（平局由后续“最少叶数”规则统一裁决）。

整棵树的节点数、叶数都不超过 128，样品不超过 500，复杂度约为
O(节点数 × max_leaves² × 子树样品数)，实测毫秒级；剪枝组合从不枚举。
"""

from .tree import READING_MAX, READING_MIN, classify, count_leaves, validate_tree_full, _is_strict_int

MIN_MAX_LEAVES = 1
MAX_SAMPLES = 500
SAMPLE_FIELDS = ("temperature", "front", "back", "category", "locked")


# ---------------------------------------------------------------- 入参校验
def validate_samples_payload(payload):
    """校验剪枝请求中的叶数上限与样品列表。

    返回 (max_leaves, samples)；samples 规范化为 dict 列表，locked 为 bool。
    任何不合法输入抛出 ValueError（中文原因）。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if "max_leaves" not in payload:
        raise ValueError("缺少叶数上限 max_leaves")
    max_leaves = payload["max_leaves"]
    if not _is_strict_int(max_leaves):
        raise ValueError("叶数上限必须是整数（不接受布尔值）")
    if not MIN_MAX_LEAVES <= max_leaves <= 128:
        raise ValueError("叶数上限必须在 1..128 闭区间内")

    if "samples" not in payload:
        raise ValueError("缺少样品列表 samples")
    samples = payload["samples"]
    if not isinstance(samples, list):
        raise ValueError("样品列表必须是数组")
    if not 1 <= len(samples) <= MAX_SAMPLES:
        raise ValueError(f"样品数量必须在 1..{MAX_SAMPLES} 条之间，当前为 {len(samples)} 条")

    normalized = []
    for idx, sample in enumerate(samples):
        where = f"第 {idx + 1} 条样品"
        if not isinstance(sample, dict):
            raise ValueError(f"{where}必须是 JSON 对象")
        extra = [k for k in sample if k not in SAMPLE_FIELDS]
        missing = [k for k in SAMPLE_FIELDS if k not in sample]
        if missing:
            raise ValueError(f"{where}缺少字段：" + "、".join(missing))
        if extra:
            raise ValueError(f"{where}包含不允许的字段：" + "、".join(extra))
        for name in ("温度", "前压", "后压"):
            key = {"温度": "temperature", "前压": "front", "后压": "back"}[name]
            value = sample[key]
            if not _is_strict_int(value):
                raise ValueError(f"{where}的{name}必须是整数（不接受布尔值）")
            if not READING_MIN <= value <= READING_MAX:
                raise ValueError(f"{where}的{name}必须在 {READING_MIN}..{READING_MAX} 闭区间内")
        category = sample["category"]
        if not isinstance(category, str) or not category.strip():
            raise ValueError(f"{where}的复验类别 category 必须是非空字符串")
        locked = sample["locked"]
        if not isinstance(locked, bool):
            raise ValueError(f"{where}的是否锁定 locked 必须是布尔值 true/false")
        normalized.append({
            "temperature": sample["temperature"],
            "front": sample["front"],
            "back": sample["back"],
            "category": category,
            "locked": locked,
        })
    return max_leaves, normalized


# ---------------------------------------------------------------- 树索引化
class _Node:
    """剪枝 DP 使用的可变节点视图，保留与原 JSON 节点的对应关系。"""

    __slots__ = ("node_id", "raw", "is_leaf", "feature", "threshold",
                 "left", "right", "leaf_category", "labels",
                 "samples", "locked_preds", "dp")

    def __init__(self, node_id, raw):
        self.node_id = node_id
        self.raw = raw
        self.is_leaf = "leaf" in raw
        self.feature = None if self.is_leaf else raw["feature"]
        self.threshold = None if self.is_leaf else raw["threshold"]
        self.left = None
        self.right = None
        self.leaf_category = raw["leaf"] if self.is_leaf else None
        # 该子树“原有叶子”的类别集合（剪枝收成叶子时只能从这里选）。
        self.labels = set()
        # 所有路由经过本节点的样品（含更深层节点，根节点持有全部样品）。
        self.samples = []
        # 经过本节点的锁定样品在【剪枝前候选树】上的预测类别集合。
        self.locked_preds = set()
        # dp: {leaf_count: {key: (misclass, choice, detail)}}
        self.dp = {}


def _index_tree(root_raw):
    """建节点索引，nodes 为后序（子节点先于父节点），可直接自底向上 DP。"""
    nodes = []

    def walk(raw):
        node = _Node(len(nodes), raw)
        # 先递归子树，再登记本节点，保证 nodes 为严格后序。
        if not node.is_leaf:
            node.left = walk(raw["left"])
            node.right = walk(raw["right"])
        nodes.append(node)
        return node

    root = walk(root_raw)
    return nodes, root


# ---------------------------------------------------------------- 动态规划
def _put_state(dp, leaf_count, key, misclass, choice, detail):
    """写入/更优替换一个 DP 状态；同 (叶数, key) 只留误分最小的。

    误分相同的状态保留先写入者，而叶数维度的最终选择由调用方按
    “误分最少、再叶数最少”统一裁决，保证结果确定。
    """
    bucket = dp.setdefault(leaf_count, {})
    old = bucket.get(key)
    if old is None or misclass < old[0]:
        bucket[key] = (misclass, choice, detail)


def _prune_dp(root_node, nodes, max_leaves):
    """自底向上执行 DP，返回根节点是否存在可行解。"""
    for node in nodes:  # nodes 为后序，子节点已就绪
        if node.is_leaf:
            # 叶子无法再剪：单叶子树，类别只能是它自己。
            mis = sum(1 for s in node.samples if s["category"] != node.leaf_category)
            node.dp = {1: {"leaf": (mis, "keep_leaf", node.leaf_category)}}
            continue

        dp = {}

        # 选项 A：把整棵子树收成一个叶子，类别 c 必须取自原子树叶子类别，
        # 且所有经过本节点的锁定样品剪枝前后预测一致。
        for c in sorted(node.labels):
            if any(pred != c for pred in node.locked_preds):
                continue
            mis = sum(1 for s in node.samples if s["category"] != c)
            _put_state(dp, 1, ("collapse", c), mis, "collapse", c)

        # 选项 B：保留原分支，左右子树各自取可行剪枝，叶数相加、误分相加。
        l_dp, r_dp = node.left.dp, node.right.dp
        for k1, l_bucket in l_dp.items():
            l_best_key = min(l_bucket, key=lambda key: l_bucket[key][0])
            l_mis = l_bucket[l_best_key][0]
            for k2, r_bucket in r_dp.items():
                k = k1 + k2
                if k > max_leaves:
                    continue
                r_best_key = min(r_bucket, key=lambda key: r_bucket[key][0])
                r_mis = r_bucket[r_best_key][0]
                _put_state(
                    dp, k, ("split", k1, l_best_key, k2, r_best_key),
                    l_mis + r_mis, "split",
                    (k1, l_best_key, k2, r_best_key),
                )
        node.dp = dp

    return bool(root_node.dp)


def _best_root_state(root_node, max_leaves):
    """在根的可行状态中按“误分最少、叶数最少”选最优，返回 (k, key, state)。"""
    candidates = []
    for k, bucket in root_node.dp.items():
        if k > max_leaves:
            continue
        for key, state in bucket.items():
            candidates.append((state[0], k, key, state))
    if not candidates:
        return None
    _, k, key, state = min(candidates, key=lambda item: (item[0], item[1]))
    return k, key, state


def _rebuild(node, leaf_count, key):
    """按回溯指针重建剪枝后的树 JSON。"""
    state = node.dp[leaf_count][key]
    choice = state[1]
    if node.is_leaf or choice in ("keep_leaf", "collapse"):
        return {"leaf": state[2]}
    # choice == "split"：detail = (k1, l_key, k2, r_key)
    k1, l_key, k2, r_key = state[2]
    return {
        "feature": node.raw["feature"],
        "threshold": node.raw["threshold"],
        "left": _rebuild(node.left, k1, l_key),
        "right": _rebuild(node.right, k2, r_key),
    }


# ---------------------------------------------------------------- 主入口
def prune_tree(candidate_tree, max_leaves, samples):
    """执行一次剪枝求解。

    返回 dict：
      status: "solved" | "no_solution" | "error"
      pruned_tree: 剪枝后树 JSON（无解时为 None）
      leaves_before / leaves_after: 剪枝前后叶数
      misclass_before / misclass_after: 剪枝前后误分样品数
      locked_total / locked_violations: 锁定样品数与锁定违约数（恒为 0）
      sample_count / node_count / max_leaves: 统计
      reason: 中文说明
    """
    base = {
        "status": "error",
        "pruned_tree": None,
        "leaves_before": 0,
        "leaves_after": 0,
        "misclass_before": 0,
        "misclass_after": 0,
        "locked_total": 0,
        "locked_violations": 0,
        "sample_count": len(samples),
        "node_count": 0,
        "max_leaves": max_leaves,
        "reason": "",
    }

    # 防御性完整校验（正常链路上游已校验过）。
    validate_tree_full(candidate_tree)
    leaves_before = count_leaves(candidate_tree)
    base["leaves_before"] = leaves_before

    # 剪枝前每条样品在候选树上的预测（锁定样品必须保持该预测）。
    readings = [
        {"temperature": s["temperature"], "front": s["front"], "back": s["back"]}
        for s in samples
    ]
    preds_before = [classify(candidate_tree, r) for r in readings]
    mis_before = sum(
        1 for s, pred in zip(samples, preds_before) if s["category"] != pred
    )
    base["misclass_before"] = mis_before
    base["locked_total"] = sum(1 for s in samples if s["locked"])

    nodes, root = _index_tree(candidate_tree)
    base["node_count"] = len(nodes)

    # 把样品挂到其路由经过的每个节点上，并收集子树原有叶子类别。
    for s, pred in zip(samples, preds_before):
        cur = root
        reading = {"temperature": s["temperature"], "front": s["front"], "back": s["back"]}
        while True:
            cur.samples.append(s)
            if s["locked"]:
                cur.locked_preds.add(pred)
            if cur.is_leaf:
                break
            cur = cur.left if reading[cur.feature] <= cur.threshold else cur.right
    # nodes 为后序（子节点在前），正序遍历即自底向上聚合子树原有叶子类别。
    for node in nodes:
        if node.is_leaf:
            node.labels.add(node.leaf_category)
        else:
            node.labels.update(node.left.labels)
            node.labels.update(node.right.labels)

    feasible = _prune_dp(root, nodes, max_leaves)
    if not feasible:
        base["status"] = "no_solution"
        base["reason"] = (
            f"无解：锁定样品的预测约束下，这棵 {leaves_before} 叶的树无法剪成 "
            f"{max_leaves} 个以内的叶子（保留原树即 {leaves_before} 叶，已超过上限）"
        )
        return base

    best = _best_root_state(root, max_leaves)
    if best is None:
        base["status"] = "no_solution"
        base["reason"] = f"无解：不存在叶数不超过 {max_leaves} 的可行剪枝"
        return base
    k, key, _state = best
    pruned = _rebuild(root, k, key)

    # 独立复核（不信任 DP 自身）：叶数、锁定约束、误分数。
    leaves_after = count_leaves(pruned)
    if leaves_after > max_leaves:  # 理论不可达，防御性断言
        base["reason"] = f"内部错误：产出叶数 {leaves_after} 超过上限 {max_leaves}"
        return base
    preds_after = [classify(pruned, r) for r in readings]
    locked_violations = sum(
        1 for s, a, b in zip(samples, preds_before, preds_after)
        if s["locked"] and a != b
    )
    if locked_violations:
        base["reason"] = f"内部错误：{locked_violations} 条锁定样品的预测发生变化"
        return base
    mis_after = sum(
        1 for s, pred in zip(samples, preds_after) if s["category"] != pred
    )

    base.update({
        "status": "solved",
        "pruned_tree": pruned,
        "leaves_after": leaves_after,
        "misclass_after": mis_after,
        "locked_violations": 0,
        "reason": (
            f"剪枝成功：叶数 {leaves_before} → {leaves_after}（上限 {max_leaves}），"
            f"误分样品 {mis_before} → {mis_after}（共 {len(samples)} 条，"
            f"其中锁定 {base['locked_total']} 条，预测全部保持不变）；"
            "误分相同时已优先选择叶数更少的剪枝"
        ),
    })
    return base
