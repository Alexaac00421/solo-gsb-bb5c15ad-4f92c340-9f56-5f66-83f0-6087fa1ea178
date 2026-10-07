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
4. **逐点水源归属**：增广过程中的源边增量无法直接归属到某个点（后期增广
   会借反向边重排前期流量的路径），因此归属在全部增广结束后，对**最终有效
   流量**做 s-t 路径分解：把每条“水源→…→用水点”的实际正向流记账给该
   (水源, 用水点)；不触碰源/汇的环流只占用管段容量、不计入任何点的归属。
   由此保证：各点 by_source 之和等于该点 supplied；各水源跨点合计等于
   source_utilization.used；被记账的水源必然沿开启的单向管段实际可达该点。
5. 未知/错类设施、重复/缺失条目、保障量超需求一次性带定位 400 拒绝；
   负数/非有限数由 Pydantic 层 422 拒绝；过期修订号 409 拒绝。
   任何失败都**不返回混合版本结果**；预演只读：不改变管网、阀位、
   复水计划或修订号。
"""
from __future__ import annotations

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


def _decompose_by_source(
    dinic: Dinic,
    super_source: int,
    super_sink: int,
    demand_ids: list[str],
) -> dict[str, dict[str, float]]:
    """把最终有效流量分解为“水源→…→用水点”路径并逐点归属（原始浮点值）。

    Dinic 残边反向容量即该边的实际正向流。反复从超级源沿仍带正流的边走到
    超级汇，抽出该路径上的瓶颈流量并记账；途中遇到环则先消环（环流不触碰
    源/汇，不归属任何点）。流守恒保证只要源边还有正流就一定能走到汇。

    返回 {demand_id: {source_id: amount}}。只沿带标签的正向边（水源边/
    管段边/汇边）走，残量增广产生的反向边无标签、不参与，故每条被记账的
    水源都沿开启的单向管段实际可达该用水点。
    """
    # node -> 流出该点且仍带正流的标签边（source/pipe/demand → sink）
    out: dict[int, list[Any]] = {u: [] for u in range(dinic.n)}
    for u in range(dinic.n):
        for e in dinic.g[u]:
            if e.tag is not None and dinic.g[e.to][e.rev].cap > EPS:
                out[u].append(e)

    by_source: dict[str, dict[str, float]] = {did: {} for did in demand_ids}

    while True:
        sid_edge = next(
            (e for e in out[super_source]
             if dinic.g[e.to][e.rev].cap > EPS),
            None,
        )
        if sid_edge is None:
            break  # 所有源边正流已分解完毕

        node_path = [super_source]
        edge_path: list[Any] = []
        while node_path[-1] != super_sink:
            u = node_path[-1]
            chosen = next(
                (e for e in out[u] if dinic.g[e.to][e.rev].cap > EPS), None
            )
            if chosen is None:
                # 流守恒下不会发生；防御性退出，避免死循环
                break
            if chosen.to in node_path:
                # 消去环：沿环各边扣减环上瓶颈流量，路径截断到环起点后重走
                cyc_start = node_path.index(chosen.to)
                cyc_edges = edge_path[cyc_start:] + [chosen]
                cyc_f = min(dinic.g[e.to][e.rev].cap for e in cyc_edges)
                for e in cyc_edges:
                    dinic.g[e.to][e.rev].cap -= cyc_f
                del node_path[cyc_start:]
                del edge_path[cyc_start:]
                continue
            node_path.append(chosen.to)
            edge_path.append(chosen)
        else:
            f = min(dinic.g[e.to][e.rev].cap for e in edge_path)
            for e in edge_path:
                dinic.g[e.to][e.rev].cap -= f
            sid = edge_path[0].tag["id"]       # 超级源->水源 边
            did = edge_path[-1].tag["id"]      # 用水点->超级汇 边
            acc = by_source[did]
            acc[sid] = acc.get(sid, 0.0) + f
            continue
        break  # 防御性分支：中途走不通则停止分解

    return by_source


def _settle_attribution(
    by_source: dict[str, dict[str, float]],
    row_ids: list[str],
    col_ids: list[str],
    row_totals: dict[str, float],
    col_totals: dict[str, float],
) -> dict[str, dict[str, float]]:
    """把归属矩阵舍入到 9 位小数，并**精确**对齐行/列合计。

    行合计=各点 supplied，列合计=各水源 used（均已独立做 9 位舍入）。

    做法（只在已存在的“可达水源”归属格上调整，不新增格、不让格变负）：
    1. 每个归属格独立四舍五入到 1e-9 整数单位 q_rc。每格相对真实值的偏差
       ∈ (-0.5, 0.5] 单位，故每条行/列的初始残差 |该轴残差| ≤ 该轴格子数；
    2. 交替扫描行、列：某轴欠缺（残差 >0）时把缺口加到该轴现值最大的已有格；
       富余（残差 <0）时从最大格起逐格扣到 0 为止。每次修正都把该轴残差清零，
       新残差只会出现在与之交叉的另一轴上，且其绝对值不超过被搬动的单位数；
    3. 初始残差有界、每轮把单位从富余轴搬向欠缺轴，配合总量锚点（行、列目标
       总量先对齐），交替扫描在有限步内同时归零；循环上限与末尾对账断言兜底。

    输入由同一最终流分解而来、行/列总量相等且每点的水源支撑真实可达，可行
    解必然存在；若出现未预期情形，末尾断言会显式报错而不返回对不上账的归属。
    """
    unit = 1e-9
    target_row = {rid: int(round(row_totals[rid] / unit)) for rid in row_ids}
    target_col = {cid: int(round(col_totals[cid] / unit)) for cid in col_ids}

    # 行、列目标总量一致才能同时对齐；二者来自同一总流量，正常无差。
    total_diff = sum(target_row.values()) - sum(target_col.values())
    if total_diff and col_ids:
        anchor = max(col_ids, key=lambda c: target_col[c])
        target_col[anchor] += total_diff

    val: dict[tuple[str, str], int] = {}
    for rid in row_ids:
        for cid, v in by_source.get(rid, {}).items():
            iv = int(round(v / unit))
            if iv:
                val[(rid, cid)] = iv

    def line_cells(k: str, by_row: bool) -> list[tuple[str, str]]:
        return [
            (r, c) for (r, c) in val
            if (r == k if by_row else c == k)
        ]

    def fix_axis(by_row: bool) -> bool:
        moved = False
        ids = row_ids if by_row else col_ids
        targets = target_row if by_row else target_col
        for k in ids:
            cells_k = line_cells(k, by_row)
            diff = targets[k] - sum(val[x] for x in cells_k)
            if diff == 0 or not cells_k:
                continue
            if diff > 0:
                # 欠缺：把缺口加到该轴现值最大的已有格（保持该点水源集合不变）
                x = max(cells_k, key=lambda z: val[z])
                val[x] += diff
            else:
                # 富余：从最大格起逐格扣减，任何格不得变负
                need = -diff
                for x in sorted(cells_k, key=lambda z: -val[z]):
                    take = min(need, val[x])
                    if take:
                        val[x] -= take
                        need -= take
                    if need == 0:
                        break
            moved = True
        return moved

    # 初始总残差有界；交替扫描在该界内归零。上限取一个宽松上界兜底。
    n_cells = max(1, len(val))
    for _ in range(4 * n_cells + 4 * (len(row_ids) + len(col_ids)) + 100):
        r_moved = fix_axis(True)
        c_moved = fix_axis(False)
        if not r_moved and not c_moved:
            break

    # 防御性自检：行/列合计必须与 supplied/used 精确一致（9 位单位）。
    # 输入由同一最终流分解而来、支撑真实可达，理论上恒成立；若触发说明
    # 出现未预期的舍入情形，宁可显式报错也不返回对不上账的归属。
    for rid in row_ids:
        assert sum(iv for (r, _c), iv in val.items() if r == rid) \
            == target_row[rid], ("attribution row", rid)
    for cid in col_ids:
        assert sum(iv for (_r, c), iv in val.items() if c == cid) \
            == target_col[cid], ("attribution col", cid)

    out: dict[str, dict[str, float]] = {rid: {} for rid in row_ids}
    for (rid, cid), iv in val.items():
        if iv > 0:
            out[rid][cid] = round(iv * unit, 9)
    return out


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
    # 后期增广会借管段反向边重排前期流量，故逐点水源归属不在增广时记账，
    # 而是在全部增广结束后对最终有效流量做 s-t 路径分解（见 _decompose_by_source）。
    index = {nid: i for i, nid in enumerate(network.nodes)}
    super_source = len(index)
    super_sink = super_source + 1
    dinic = Dinic(super_sink + 1)

    source_edges: dict[str, Any] = {}
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        if cap > EPS:
            e_idx = len(dinic.g[super_source])
            dinic.add_edge(
                super_source, index[sid], cap, {"kind": "source", "id": sid}
            )
            source_edges[sid] = dinic.g[super_source][e_idx]
    pipe_edges: dict[str, Any] = {}
    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe) and pipe.capacity > EPS:
            u = index[pipe.from_node]
            e_idx = len(dinic.g[u])
            dinic.add_edge(
                u, index[pipe.to_node], pipe.capacity, {"kind": "pipe", "id": pid}
            )
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

    demand_id_order = [n.id for n in demands]
    supplied_by_id: dict[str, float] = {
        did: round(flow_of(did), 9) for did in demand_id_order
    }

    # 水源用量须在路径分解（会改写残边反向容量）之前从正向源边读取
    used_by_source: dict[str, float] = {}
    source_utilization = []
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        e = source_edges.get(sid)
        used = round(cap - e.cap, 9) if e is not None else 0.0
        remaining = round(e.cap, 9) if e is not None else 0.0
        used_by_source[sid] = used
        source_utilization.append({
            "source_id": sid,
            "capacity": cap,
            "used": used,
            "remaining": remaining,
        })

    # 逐点水源归属：对最终有效流量做 s-t 路径分解，再把 9 位舍入残差对账到
    # 行合计（各点 supplied）与列合计（各水源 used）
    raw_by_source = _decompose_by_source(
        dinic, super_source, super_sink, demand_id_order
    )
    by_source = _settle_attribution(
        raw_by_source,
        row_ids=demand_id_order,
        col_ids=network.source_ids(),
        row_totals=supplied_by_id,
        col_totals=used_by_source,
    )

    allocations: list[dict[str, Any]] = []
    for node in demands:
        need = node.demand or 0.0
        g = guarantees[node.id]
        supplied = supplied_by_id[node.id]
        allocations.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "requested": need,
            "guaranteed": round(g, 9),
            "supplied": supplied,
            "extra": round(max(0.0, supplied - g), 9),
            "shortfall": round(max(0.0, need - supplied), 9),
            "fulfilled": supplied + EPS >= need,
            "by_source": dict(sorted(by_source[node.id].items())),
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
