"""内置示例：一组可直接成功试算，另两组分别演示压差不可行与温区无安全段。"""


def _node(feature, threshold, left, right):
    return {"feature": feature, "threshold": threshold, "left": left, "right": right}


def _leaf(category):
    return {"leaf": category}


# 示例 1：可成功试算。
# 两树仅在“高温且后压偏高”时分歧（原树 REJECT、候选 REWORK）；
# 工况温区 150..300 中，150..200 全程两树一致，且 200 之后出现分歧，
# 因此最长安全温区为 [150,200]，共 51 个温度。
ORIGINAL_1 = _node(
    "temperature", 200,
    _leaf("PASS"),
    _node("back", 400, _leaf("PASS"), _leaf("REJECT")),
)
CANDIDATE_1 = _node(
    "temperature", 200,
    _leaf("PASS"),
    _node("back", 400, _leaf("PASS"), _leaf("REWORK")),
)
DRAFT_1 = {
    "temperature": [150, 300],
    "front": [100, 200],
    "back": [150, 500],
    "diff": [0, 300],
}

# 示例 2：压差不可行。后压最大 120、前压最小 100，压差至多 20，
# 无法满足 [100, 200]，任何温度都没有合规压力组合。
ORIGINAL_2 = ORIGINAL_1
CANDIDATE_2 = CANDIDATE_1
DRAFT_2 = {
    "temperature": [150, 300],
    "front": [100, 200],
    "back": [100, 120],
    "diff": [100, 200],
}

# 示例 3：压力可行但两树在工况温区每个温度上都存在分歧读数。
# 原树恒为 PASS；候选树按温度 200 分流为 A/B，温区 150..300 内
# 低温段候选为 A、高温段候选为 B，两段均与原树类别不同 → 无安全温区。
ORIGINAL_3 = _leaf("PASS")
CANDIDATE_3 = _node("temperature", 200, _leaf("A"), _leaf("B"))
DRAFT_3 = {
    "temperature": [150, 300],
    "front": [100, 200],
    "back": [150, 500],
    "diff": [0, 300],
}

EXAMPLES = [
    {
        "key": "solvable",
        "title": "示例一：可成功试算（接管温区 [150,200]）",
        "original_tree": ORIGINAL_1,
        "candidate_tree": CANDIDATE_1,
        "draft": DRAFT_1,
    },
    {
        "key": "infeasible_pressure",
        "title": "示例二：压差区间无解（无合规压力组合）",
        "original_tree": ORIGINAL_2,
        "candidate_tree": CANDIDATE_2,
        "draft": DRAFT_2,
    },
    {
        "key": "no_safe_zone",
        "title": "示例三：压力可行但无一致温区",
        "original_tree": ORIGINAL_3,
        "candidate_tree": CANDIDATE_3,
        "draft": DRAFT_3,
    },
]


# ---------------------------------------------------------------- 剪枝示例
def _p(temperature, front, back, category, locked):
    return {
        "temperature": temperature,
        "front": front,
        "back": back,
        "category": category,
        "locked": locked,
    }


# 剪枝示例用的 8 叶候选树：温度 200 → 前压 150 → 后压 300 的三层对称树。
PRUNE_TREE = _node(
    "temperature", 200,
    _node("front", 150,
          _node("back", 300, _leaf("PASS"), _leaf("REWORK")),
          _node("back", 300, _leaf("PASS"), _leaf("REJECT"))),
    _node("front", 150,
          _node("back", 300, _leaf("REWORK"), _leaf("REJECT")),
          _node("back", 300, _leaf("REJECT"), _leaf("REJECT"))),
)

# 16 条样品（在 1..500 范围内）：低温半区复验类别全为 PASS、高温半区全为
# REJECT；其中 2 条锁定样品都落在“低温-前压低-后压低”的 PASS 叶，只钉住
# 深层路径，不阻止两个半区各自收成单叶。
# 剪枝前候选树对样品有 7 条误分（树里的 REWORK/REJECT 与复验 PASS/REJECT
# 不一致）。叶数上限取 4 时，最优剪枝把每个温区半区收成一个叶子：
# 8 叶 → 2 叶，误分 7 → 0，锁定样品预测保持 PASS 不变。
PRUNE_SAMPLES_OK = [
    _p(100, 100, 200, "PASS", True),
    _p(120, 110, 210, "PASS", True),
    _p(100, 100, 400, "PASS", False),
    _p(100, 200, 200, "PASS", False),
    _p(100, 200, 400, "PASS", False),
    _p(150, 120, 250, "PASS", False),
    _p(150, 180, 350, "PASS", False),
    _p(180, 110, 420, "PASS", False),
    _p(300, 100, 200, "REJECT", False),
    _p(300, 100, 400, "REJECT", False),
    _p(300, 200, 200, "REJECT", False),
    _p(300, 200, 400, "REJECT", False),
    _p(250, 120, 250, "REJECT", False),
    _p(250, 180, 350, "REJECT", False),
    _p(280, 90, 100, "REJECT", False),
    _p(290, 220, 450, "REJECT", False),
]

# 无解示例：叶数上限 1，两条锁定样品在根处的剪枝前预测分别为 PASS 与
# REJECT，根节点无法收成任何单一类别（否则必然改变某条锁定样品的预测），
# 而保留分支至少需要 2 个叶子，超过上限 → 无解，草稿保留不变。
PRUNE_SAMPLES_LOCKED = [
    _p(100, 100, 200, "PASS", True),
    _p(300, 200, 400, "REJECT", True),
    _p(100, 100, 400, "PASS", False),
]

PRUNING_EXAMPLES = [
    {
        "key": "prune_ok",
        "title": "剪枝示例 A：8 叶剪到 2 叶、误分 7→0（上限 4，含 2 条锁定）",
        "candidate_tree": PRUNE_TREE,
        "max_leaves": 4,
        "samples": PRUNE_SAMPLES_OK,
    },
    {
        "key": "prune_locked_no_solution",
        "title": "剪枝示例 B：锁定冲突导致 1 叶上限无解（草稿不变）",
        "candidate_tree": PRUNE_TREE,
        "max_leaves": 1,
        "samples": PRUNE_SAMPLES_LOCKED,
    },
]
