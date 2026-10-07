"""抢修最低保障供水预演（只读，不写入）。

业务规则：
1. 请求携带当前管网**修订号**与**恰好覆盖所有用水点**的保障量清单；
   保障量为有限非负数（Pydantic 层保证），同一用水点不得重复，
   且不得超过该点**现有需求**。
2. **保障可行性判定**：在现有阀位、单向管段、水源上限与共享管段容量下，
   把每个用水点的保障量作为必须同时满足的最低供水，用最大流判定能否
   **同时**满足全部保障量。不可行时返回各点保障缺口与瓶颈设施
   （最小割上打满的水源/管段），**不返回任何部分分配方案**。
3. **额外供水最大化**：可行时在保持全部保障量的前提下，按现有优先级
   （数值越大越优先）及同级编号升序逐点把供水提升至需求——在残余网络上
   继续增广，允许经管段反向边**重排先前流量的路径**；汇边（用水点→超级汇）
   的反向边不在分层增广路径上，任何点的既有供水只增不减，
   单点总供水不超过其需求。
   **逐点水源归属按最终有效流量分解**：增广只负责求出最终流，阶段二结束后
   把最终流沿“超级源→水源→开启的单向管段→用水点”分解为水源-用水点路径
   （先消去有向环流再逐条取路径），按路径流量累计各点 by_source——
   增广过程中为满足后续点而经反向边重排路径时，归属随之落在最终实际供水的
   水源上；因此各点来源之和等于 supplied、各水源跨点合计等于
   source_utilization.used，且每条来源都沿开启的单向管段实际可达。
4. 未知/错类设施、重复/缺失条目、保障量超需求一次性带定位 400 拒绝；
   负数/非有限数由 Pydantic 层 422 拒绝；过期修订号 409 拒绝。
   任何失败都**不返回混合版本结果**；预演只读：不改变管网、阀位、
   复水计划或修订号。
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from .engine import EPS, Dinic, Network

_TYPE_NAMES = {"source": "水源", "demand": "用水点", "junction": "连接点"}
_KIND_NAMES = {**_TYPE_NAMES, "pipe": "管段", "valve": "阀门"}


def _facility_kind(network: Network, fid: str) -> Optional[str]:
    """设施类别：source/demand/junction/pipe/valve；不存在返回 None。"""
    node = network.nodes.get(fid)
    if node is not None:
        return node.type
    if fid in network.pipes:
        return "pipe"
    if fid in network.valves:
        return "valve"
    return None


def validate_guarantees(
    network: Network, items: list[dict[str, Any]]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """基于已提交快照逐条校验保障量条目。

    返回 (guarantees, errors)：guarantees 为 用水点id -> 保障量；
    errors 非空时映射不可用于预演（调用方应整单拒绝）。
    """
    errors: list[dict[str, Any]] = []
    guarantees: dict[str, float] = {}
    seen: set[str] = set()
    for i, item in enumerate(items):
        loc = f"guarantees[{i}]"
        did = item["demand_id"]
        amount = float(item["amount"])

        if did in seen:
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "duplicate_facility",
                "facility_id": did,
                "message": f"条目重复：{did} 在 guarantees 中出现多次",
            })
            continue
        seen.add(did)

        kind = _facility_kind(network, did)
        if kind is None:
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "unknown_facility",
                "facility_id": did,
                "message": f"未知设施：{did}（当前管网中不存在该用水点）",
            })
            continue
        if kind != "demand":
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "wrong_facility_type",
                "facility_id": did,
                "message": (
                    f"设施类别错误：{did} 是{_KIND_NAMES[kind]}，"
                    "不能作为用水点条目（请核对编号）"
                ),
            })
            continue

        need = network.nodes[did].demand or 0.0
        # 保障量不得超过该点现有需求（1e-9 容差，吸收浮点序列化误差）
        if amount > need + EPS:
            errors.append({
                "location": f"{loc}.amount",
                "code": "guarantee_exceeds_demand",
                "facility_id": did,
                "demand": need,
                "submitted_guarantee": amount,
                "message": (
                    f"用水点 {did} 的保障量 {amount:g} 超过其现有需求 "
                    f"{need:g}；保障量不得大于需求"
                ),
            })
            continue
        guarantees[did] = amount

    for did in network.demand_ids():
        if did not in seen:
            errors.append({
                "location": "guarantees",
                "code": "missing_entry",
                "facility_id": did,
                "message": (
                    "缺失条目：guarantees 必须恰好列出所有用水点的保障量，"
                    f"缺少 {did}"
                ),
            })
    return guarantees, errors


def _final_flow_by_source(
    index: dict[str, int],
    source_caps: dict[str, float],
    source_edges: dict[str, Any],
    pipe_defs: dict[str, tuple[str, str, float]],
    pipe_edges: dict[str, Any],
    demand_caps: dict[str, float],
    demand_edges: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """把最终有效流分解为 用水点 -> {水源: 流量}。

    增广过程中后续点可能经管段反向边重排先前流量的路径，逐次增广的
    “源边增量”并不等于最终各点的实际来源。这里以**最终流**为准：
      1. 取源边/管段边/汇边的正向净流量（初始容量 − 残余容量）；
      2. 先消去内部图（水源/连接点/用水点之间的导通单向管段边）中的有向环流；
      3. 再把最终流拆成“水源 → … → 用水点 → 超级汇”的路径逐条记账。
         汇边也是行走的可选出边，故水流**经某用水点继续流向下一用水点**时
         不会被误记到途经点：每单位流只在其真正汇入的汇边终点记账。
    分解只使用导通单向管段的正向边，因此记账的来源都沿开启的单向管段实际
    可达；各点来源之和等于其汇边流量（supplied），各水源跨点合计等于源边
    用量（source_utilization.used）。

    pipe_defs: 管段id -> (from_node, to_node, 初始容量)，仅含流网络中的导通管段；
    demand_caps: 用水点id -> 汇边初始容量（= 该点需求）。
    """
    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(index) * 4 + 1000))

    n = len(index)
    # 内部有向边（流量可变）：eid -> [u, v, flow]
    edges: list[list[Any]] = []
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for pid, e in pipe_edges.items():
        a, b, cap0 = pipe_defs[pid]
        flow = cap0 - e.cap
        if flow > EPS:
            adj[index[a]].append(len(edges))
            edges.append([index[a], index[b], flow])

    def find_cycle() -> Optional[list[int]]:
        """在内部有向正流图中找一个环（边 id 序列）；无环返回 None。"""
        color = [0] * n  # 0 白 / 1 灰（在递归栈上）/ 2 黑
        stack_eids: list[int] = []

        def dfs(u: int) -> Optional[list[int]]:
            color[u] = 1
            for eid in adj[u]:
                if edges[eid][2] <= EPS:
                    continue
                v = edges[eid][1]
                if color[v] == 0:
                    stack_eids.append(eid)
                    found = dfs(v)
                    if found is not None:
                        return found
                    stack_eids.pop()
                elif color[v] == 1:
                    # 回边构成环：栈中从起点为 v 的边到末尾，加上当前回边
                    cut = next(
                        k for k, e in enumerate(stack_eids) if edges[e][0] == v
                    )
                    return stack_eids[cut:] + [eid]
            color[u] = 2
            return None

        for s in range(n):
            if color[s] == 0:
                cyc = dfs(s)
                if cyc is not None:
                    return cyc
        return None

    # 消去全部有向环流：环流不经过任何汇边，不贡献任何点的实际供水
    while True:
        cyc = find_cycle()
        if cyc is None:
            break
        f = min(edges[eid][2] for eid in cyc)
        for eid in cyc:
            edges[eid][2] -= f

    node_of = {v: k for k, v in index.items()}
    by_source: dict[str, dict[str, float]] = {did: {} for did in demand_edges}
    # 各汇边尚存的净流量（行走选中汇边时按此扣减）
    sink_remaining = {
        did: demand_caps[did] - e.cap for did, e in demand_edges.items()
    }

    # 从每个水源出发，在无环正流图上反复取一条到超级汇的路径并扣除流量。
    # 节点守恒保证：正流量要么沿管段继续，要么（在用水点）经汇边结束；
    # 源出流量合计等于汇边流量合计，故全部水源 amount 取完时汇边也恰好用完。
    for sid, e in source_edges.items():
        amount = source_caps[sid] - e.cap
        if amount <= EPS:
            continue
        u = index[sid]
        while amount > EPS:
            path_eids: list[int] = []
            cur = u
            end_did: Optional[str] = None
            while True:
                cur_id = node_of[cur]
                # 到达用水点且其汇边尚有未记账流量：优先在此汇入超级汇；
                # 汇边流量已取完时，则沿出边继续（该点只是水流途经点）。
                if sink_remaining.get(cur_id, 0.0) > EPS:
                    end_did = cur_id
                    break
                nxts = [eid for eid in adj[cur] if edges[eid][2] > EPS]
                # 守恒保证：未到汇点的正流量节点必有正流量出边
                eid = nxts[0]
                path_eids.append(eid)
                cur = edges[eid][1]
            f = min([amount, sink_remaining[end_did]]
                    + [edges[eid][2] for eid in path_eids])
            for eid in path_eids:
                edges[eid][2] -= f
            sink_remaining[end_did] -= f
            amount -= f
            acc = by_source[end_did]
            acc[sid] = acc.get(sid, 0.0) + f

    # 与输出同口径四舍五入到 9 位小数，并丢弃归零项
    return {
        did: {s: round(v, 9) for s, v in acc.items() if round(v, 9) > 0}
        for did, acc in by_source.items()
    }


def preview(
    network: Network,
    revision: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """两阶段保障预演：先判定全部保障量能否同时满足，再逐点最大化额外供水。

    调用方须先完成修订号校验与 validate_guarantees 条目校验。
    """
    guarantees, _errors = validate_guarantees(network, items)

    # 处理顺序：优先级降序，同级按编号升序（与 engine.simulate 同口径）
    demands = sorted(
        (network.nodes[did] for did in network.demand_ids()),
        key=lambda n: (-(n.priority or 0), n.id),
    )

    # 流网络：超级源 -> 水源（上限）-> 单向导通管段（容量）-> 用水点 -> 超级汇。
    # 汇边初始容量 0：保障量与额外供水都通过“提额 + 残余增广”逐点汇入；
    # 逐点 by_source 不按逐次增广增量记账（增广可能经反向边重排先前路径），
    # 而是在最终流上统一分解（见 _final_flow_by_source）。
    index = {nid: i for i, nid in enumerate(network.nodes)}
    super_source = len(index)
    super_sink = super_source + 1
    dinic = Dinic(super_sink + 1)

    source_caps: dict[str, float] = {}
    source_edges: dict[str, Any] = {}
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        if cap > EPS:
            e_idx = len(dinic.g[super_source])
            dinic.add_edge(
                super_source, index[sid], cap, {"kind": "source", "id": sid}
            )
            source_caps[sid] = cap
            source_edges[sid] = dinic.g[super_source][e_idx]
    pipe_defs: dict[str, tuple[str, str, float]] = {}
    pipe_edges: dict[str, Any] = {}
    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe) and pipe.capacity > EPS:
            u = index[pipe.from_node]
            e_idx = len(dinic.g[u])
            dinic.add_edge(
                u, index[pipe.to_node], pipe.capacity, {"kind": "pipe", "id": pid}
            )
            pipe_defs[pid] = (pipe.from_node, pipe.to_node, pipe.capacity)
            pipe_edges[pid] = dinic.g[u][e_idx]
    demand_edges: dict[str, Any] = {}
    demand_cap: dict[str, float] = {}
    for node in demands:
        u = index[node.id]
        e_idx = len(dinic.g[u])
        dinic.add_edge(u, super_sink, 0.0, {"kind": "demand", "id": node.id})
        demand_edges[node.id] = dinic.g[u][e_idx]
        demand_cap[node.id] = 0.0

    def augment_to(node: Any, target: float) -> float:
        """把该点汇边容量提升到 target 并在残余图上继续增广。

        返回本次新增流量。管段反向边允许重排先前流量的路径；
        汇边反向边从超级汇出发、不在分层增广路径上，
        因此任何点的既有供水只增不减（保障量必被保持）。
        """
        edge = demand_edges[node.id]
        add = target - demand_cap[node.id]
        if add > EPS:
            edge.cap += add
            demand_cap[node.id] = target
        return dinic.max_flow(super_source, super_sink)

    def flow_of(did: str) -> float:
        return demand_cap[did] - demand_edges[did].cap

    # ---- 阶段一：逐点汇入保障量，判定全部保障量能否同时满足 ----
    for node in demands:
        augment_to(node, guarantees[node.id])

    shortfalls: list[dict[str, Any]] = []
    total_guaranteed = 0.0
    total_suppliable = 0.0
    for node in demands:
        g = guarantees[node.id]
        attained = flow_of(node.id)
        total_guaranteed += g
        total_suppliable += attained
        shortfalls.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "guaranteed": round(g, 9),
            "suppliable": round(attained, 9),
            "shortfall": round(max(0.0, g - attained), 9),
        })

    if any(s["shortfall"] > EPS for s in shortfalls):
        # 不可行：最小割上打满的水源/管段即瓶颈设施；不返回任何部分分配方案
        reachable = dinic.reachable_from_source(super_source)
        bottlenecks: list[dict[str, Any]] = []
        for sid in network.source_ids():
            e = source_edges.get(sid)
            if e is None:
                continue  # 上限为 0 的水源不在流网络中，不属于“打满”的割边
            if index[sid] not in reachable and e.cap <= EPS:
                cap = network.nodes[sid].capacity or 0.0
                bottlenecks.append({
                    "kind": "source",
                    "facility_id": sid,
                    "capacity": cap,
                    "used": round(cap - e.cap, 9),
                    "remaining": round(e.cap, 9),
                })
        for pid, pipe in sorted(network.pipes.items()):
            e = pipe_edges.get(pid)
            if e is None:
                continue  # 不导通或容量为 0 的管段不在流网络中
            if (
                index[pipe.from_node] in reachable
                and index[pipe.to_node] not in reachable
                and e.cap <= EPS
            ):
                bottlenecks.append({
                    "kind": "pipe",
                    "facility_id": pid,
                    "from_node": pipe.from_node,
                    "to_node": pipe.to_node,
                    "capacity": pipe.capacity,
                    "used": round(pipe.capacity - e.cap, 9),
                    "remaining": round(e.cap, 9),
                    "closed_valves": sorted(
                        vid
                        for vid in network.valves_by_pipe.get(pid, ())
                        if not network.valves[vid].is_open
                    ),
                })
        return {
            "message": "保障预演完成：当前阀位与容量下全部保障量无法同时满足，"
            "未给出任何部分分配方案；本次结果为临时计算，"
            "未改变管网、阀位、计划或修订号",
            "revision": revision,
            "feasible": False,
            "guarantee_shortfalls": shortfalls,
            "totals": {
                "guaranteed": round(total_guaranteed, 9),
                "suppliable": round(total_suppliable, 9),
                "shortfall": round(
                    max(0.0, total_guaranteed - total_suppliable), 9
                ),
            },
            "bottleneck_facilities": bottlenecks,
        }

    # ---- 阶段二：保持全部保障量，按优先级逐点最大化额外供水 ----
    for node in demands:
        augment_to(node, node.demand or 0.0)

    # 逐点水源归属：按**最终有效流**沿开启的单向管段分解，
    # 而非逐次增广的源边增量（后者在反向边重排路径后会错配归属）。
    by_source = _final_flow_by_source(
        index, source_caps, source_edges, pipe_defs, pipe_edges,
        demand_cap, demand_edges,
    )

    allocations: list[dict[str, Any]] = []
    for node in demands:
        need = node.demand or 0.0
        g = guarantees[node.id]
        supplied = round(flow_of(node.id), 9)
        allocations.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "requested": need,
            "guaranteed": round(g, 9),
            "supplied": supplied,
            "extra": round(max(0.0, supplied - g), 9),
            "shortfall": round(max(0.0, need - supplied), 9),
            "fulfilled": supplied + EPS >= need,
            "by_source": dict(sorted(by_source.get(node.id, {}).items())),
        })

    source_utilization = []
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        e = source_edges.get(sid)
        remaining = e.cap if e is not None else 0.0
        source_utilization.append({
            "source_id": sid,
            "capacity": cap,
            "used": round(cap - remaining, 9),
            "remaining": round(remaining, 9),
        })
    pipe_utilization = []
    for pid, pipe in sorted(network.pipes.items()):
        conducting = network.pipe_is_open(pipe)
        e = pipe_edges.get(pid)
        if e is not None:
            remaining = e.cap
        else:
            # 不在流网络中：不导通管段容量未被占用；导通但容量为 0 则剩余 0
            remaining = pipe.capacity if not conducting else 0.0
        pipe_utilization.append({
            "pipe_id": pid,
            "capacity": pipe.capacity,
            "used": round(pipe.capacity - remaining, 9),
            "remaining": round(remaining, 9),
            "conducting": conducting,
        })

    return {
        "message": "保障预演完成：全部保障量可同时满足，"
        "额外供水已按优先级逐点最大化；本次结果为临时计算，"
        "未改变管网、阀位、计划或修订号",
        "revision": revision,
        "feasible": True,
        "allocations": allocations,
        "totals": {
            "requested": round(sum(a["requested"] for a in allocations), 9),
            "guaranteed": round(sum(a["guaranteed"] for a in allocations), 9),
            "supplied": round(sum(a["supplied"] for a in allocations), 9),
            "extra": round(sum(a["extra"] for a in allocations), 9),
            "shortfall": round(sum(a["shortfall"] for a in allocations), 9),
        },
        "source_utilization": source_utilization,
        "pipe_utilization": pipe_utilization,
    }
