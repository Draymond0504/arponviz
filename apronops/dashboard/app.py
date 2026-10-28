"""Streamlit 看板：运行总览 / 异常处置 / 运营复盘。"""
from __future__ import annotations

import os
import uuid

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

API_BASE = os.environ.get("APRONOPS_API_BASE", "http://localhost:8000")

st.set_page_config(page_title="ApronOps", layout="wide")


def api_get(path: str, **params):
    r = requests.get(f"{API_BASE}{path}", params=params, timeout=5)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json=None, **params):
    r = requests.post(f"{API_BASE}{path}", json=json, params=params, timeout=5)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=10)
def get_layout_cached():
    # 机坪拓扑运行期不变，缓存以省一次往返
    return api_get("/layout")


# 以下色值取自 dataviz 技能的默认调色板（light 模式）
HIGHLIGHT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]  # 分类色槎位 1-3，仅供用户主动高亮的最多 3 辆车使用
EDGE_COLOR = "#e1e0d9"
NODE_FILL_COLOR = "#c3c2b7"
INK_PRIMARY = "#0b0b0b"
TRACK_COLOR = "#898781"
VEHICLE_MARKER_COLOR = "#52514e"
ANOMALOUS_STATES = {"DEGRADED", "PAUSED", "SAFE_STOP", "REMOTE_ASSIST"}


def build_topology_figure(layout_data, vehicles, tracks, highlight_ids, show_aircraft=False, route=None):
    """机坪拓扑（作业点+连接边）叠加车辆轨迹与当前位置。

    车辆身份靠文字标签区分，不靠颜色——同一坐标系里任意两辆车的轨迹都可能
    相邻/交叉，调色板在这种"全两两相邻"场景下只有前 3 槎位能过色觉校验，
    因此默认全部车辆用统一灰色尾迹，仅用户主动勾选（最多 3 辆）时才换成
    分类色高亮。
    """
    fig = go.Figure()

    nodes = layout_data["nodes"]
    node_by_id = {n["id"]: n for n in nodes}
    reference = layout_data.get("reference", {})
    ref_nodes = {n["id"]: n for n in reference.get("nodes", [])}
    if show_aircraft:
        xs, ys = [], []
        for e in reference.get("edges", []):
            a, b = ref_nodes[e["from"]], ref_nodes[e["to"]]
            xs.extend([a["x"], b["x"], None])
            ys.extend([a["y"], b["y"], None])
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color="#c6cbd1", width=1, dash="dot"),
                                 name="飞机网络（仅参照）", hoverinfo="skip"))
    stands = [n for n in ref_nodes.values() if n["type"] == "stand"]
    if stands:
        fig.add_trace(go.Scatter(
            x=[n["x"] for n in stands], y=[n["y"] for n in stands],
            mode="markers", marker=dict(symbol="square-open", size=12, color="#8c98a4"),
            text=[n["label"] for n in stands], hovertemplate="%{text} · 飞机停靠位置<extra></extra>",
            name="机位位置（Layout H）"))
    road_styles = {
        "perimeter": ("外围主路 · 20 km/h", "#485d70", 5),
        "service": ("分区服务路 · 12 km/h", "#408b91", 3),
        "stand_access": ("作业支路 · 5 km/h", "#a4bbc1", 2),
    }
    seen_edges = set()
    seen_types = set()
    for e in layout_data["edges"]:
        key = frozenset((e["from"], e["to"]))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        a, b = node_by_id[e["from"]], node_by_id[e["to"]]
        road_type = e.get("road_type", "service")
        name, color, width = road_styles[road_type]
        if e.get("closed"):
            name, color, width, road_type = "封闭路段", "#bf3f3f", 6, "closed"
        fig.add_trace(go.Scatter(
            x=[a["x"], b["x"]], y=[a["y"], b["y"]],
            mode="lines", line=dict(color=color, width=width, dash="dash" if e.get("closed") else "solid"),
            text=[name, name], hovertemplate="%{text}<extra></extra>",
            name=name, legendgroup=road_type, showlegend=road_type not in seen_types,
        ))
        seen_types.add(road_type)

    styles = {
        "service_point": ("机位服务点", "circle", 7, "#2a78d6"),
        "road": ("道路连接点", "circle", 3, "#8795a2"),
        "crossing": ("交叉等待点", "diamond", 10, "#c18423"),
        "depot": ("车辆基地", "square", 12, "#1b8a64"),
        "checkpoint": ("巡检点", "triangle-up", 11, "#7a60a8"),
    }
    for kind, (name, symbol, size, color) in styles.items():
        group = [n for n in nodes if n["type"] == kind]
        if not group:
            continue
        labelled = kind in ("service_point", "depot", "checkpoint")
        fig.add_trace(go.Scatter(
            x=[n["x"] for n in group], y=[n["y"] for n in group],
            mode="markers+text" if labelled else "markers",
            marker=dict(symbol=symbol, size=size, color=color),
            text=[n["id"].removeprefix("SP_") if kind == "service_point" else n["label"] for n in group],
            customdata=[[n["id"], n["label"]] for n in group],
            textposition="top center", textfont=dict(size=10, color=INK_PRIMARY),
            hovertemplate="%{customdata[1]}<br>节点 ID: %{customdata[0]}<extra></extra>",
            name=name, showlegend=True,
        ))

    if route:
        points = [node_by_id[nid] for nid in route["path"]]
        fig.add_trace(go.Scatter(
            x=[n["x"] for n in points], y=[n["y"] for n in points], mode="lines",
            line=dict(color="#ad4fad", width=5, dash="dash"), name="预览路线",
            text=[n["label"] for n in points], hovertemplate="%{text}<extra></extra>"))

    highlight_color = {vid: HIGHLIGHT_COLORS[i] for i, vid in enumerate(highlight_ids[:3])}

    for v in vehicles:
        vid = v["id"]
        points = tracks.get(vid, [])
        is_highlighted = vid in highlight_color
        color = highlight_color.get(vid, TRACK_COLOR)
        xs, ys = [p["x"] for p in points], [p["y"] for p in points]
        if xs:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines",
                line=dict(color=color, width=2),
                opacity=1.0 if is_highlighted else 0.5,
                showlegend=is_highlighted,
                name=vid,
                hoverinfo="skip",
            ))

        is_anomalous = v["state"] in ANOMALOUS_STATES
        label = vid + ("  ⚠" if is_anomalous else "")
        fig.add_trace(go.Scatter(
            x=[v["x"]], y=[v["y"]],
            mode="markers+text",
            marker=dict(
                symbol="triangle-up" if is_anomalous else "circle",
                size=16 if is_anomalous else 12,
                color=color,
                line=dict(color="#ffffff", width=1),
            ),
            text=[label],
            textposition="bottom center",
            textfont=dict(color=INK_PRIMARY),
            hovertemplate=(
                f"车辆 {vid}<br>状态: {v['state']}<br>电量: {v['battery']:.0f}%"
                f"<br>实时速度: {v.get('current_speed_mps', 0.0):.1f} m/s"
                f"<br>当前任务: {v.get('current_task_id') or '无'}<extra></extra>"
            ),
            showlegend=False,
            name=vid,
        ))

    fig.update_layout(
        height=700,
        margin=dict(l=20, r=20, t=20, b=40),
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
        legend=dict(orientation="h", yanchor="top", y=-0.02),
        uirevision=layout_data.get("metadata", {}).get("version", "layout"),
    )
    return fig


def error_text(error):
    response = getattr(error, "response", None)
    if response is not None:
        try:
            return str(response.json().get("detail", error))
        except ValueError:
            pass
    return str(error)


def fetch_state():
    try:
        state = api_get("/state")
    except requests.RequestException as error:
        st.error(f"数据更新失败，当前状态不可用：{error_text(error)}")
        st.stop()
    if state["runtime_error"]:
        st.error(state["runtime_error"])
    st.caption(f"每 2 秒更新 · 仿真时间 {state['simulation_time']} UTC · 状态版本 {state['revision']}")
    return state


@st.fragment(run_every=2)
def live_overview(layout_data, highlight_ids, show_aircraft, route_endpoints):
    state = fetch_state()
    vehicles, tasks, alerts = state["vehicles"], state["tasks"], state["alerts"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("车辆总数", len(vehicles))
    c2.metric("执行中任务", sum(t["status"] in ("ASSIGNED", "IN_PROGRESS") for t in tasks))
    c3.metric("待分配 / 待转派", sum(t["status"] in ("PENDING", "REASSIGNED") for t in tasks))
    c4.metric("未解决告警", sum(not a["resolved_at"] for a in alerts))
    closed = {frozenset((c["from_node"], c["to_node"])) for c in state["closures"] if not c["reopened_at"]}
    current_layout = {**layout_data, "edges": [
        {**e, "closed": frozenset((e["from"], e["to"])) in closed} for e in layout_data["edges"]]}
    route = None
    if route_endpoints:
        try:
            route = api_get("/routes", origin=route_endpoints[0], destination=route_endpoints[1])
            st.caption(f"预览路线：{route['distance_m']:.0f} 米 · {len(route['crossings'])} 个交叉等待点")
        except requests.HTTPError as error:
            if error.response.status_code == 409:
                st.warning("所选起终点暂无可通行路线；道路恢复后自动更新。")
            else:
                st.warning(f"路线暂不可用：{error_text(error)}")
        except requests.RequestException as error:
            st.warning(f"路线更新失败：{error_text(error)}")
    st.plotly_chart(build_topology_figure(current_layout, vehicles, state["tracks"], highlight_ids,
                                        show_aircraft, route), use_container_width=True, key="live_map")
    st.subheader("车辆状态")
    if vehicles:
        df = pd.DataFrame(vehicles)[["id", "type", "state", "task_phase", "current_speed_mps", "battery", "current_task_id", "wait_reason"]]
        st.dataframe(df.rename(columns={"current_speed_mps": "速度(m/s)", "task_phase": "任务阶段", "wait_reason": "等待原因"}), use_container_width=True)
    st.subheader("任务列表")
    if tasks:
        st.dataframe(pd.DataFrame(tasks)[["id", "type", "status", "priority", "origin", "destination", "assigned_vehicle_id"]], use_container_width=True)


@st.fragment(run_every=2)
def live_alerts():
    state = fetch_state()
    st.subheader("告警与处置")
    if state["alerts"]:
        st.dataframe(pd.DataFrame(state["alerts"]), use_container_width=True)
    else:
        st.info("暂无告警")
    unresolved = {a["id"]: a for a in state["alerts"] if not a["resolved_at"]}
    if unresolved:
        with st.form("resolve_alert"):
            aid = st.selectbox("选择待处置告警", list(unresolved),
                               format_func=lambda k: f"{unresolved[k]['anomaly_type']} · {', '.join(unresolved[k]['impact_scope'])}")
            resolution = st.text_input("处置说明", value="人工确认恢复")
            if st.form_submit_button("确认恢复并关闭告警"):
                try:
                    api_post(f"/alerts/{aid}/resolve", json={"resolution": resolution})
                    st.success("处置已保存；车辆按任务阶段恢复，道路告警会同步解除封闭。")
                except requests.RequestException as error:
                    st.error(error_text(error))
    st.subheader("封路记录")
    if state["closures"]:
        st.dataframe(pd.DataFrame(state["closures"])[["from_node", "to_node", "reason", "closed_at", "reopened_at", "resolution"]], use_container_width=True)
    else:
        st.info("暂无道路封闭记录")
    st.subheader("近期操作与任务事件")
    try:
        events = api_get("/events")
        if events:
            st.dataframe(pd.DataFrame(events)[["timestamp", "event_type", "source", "related_task_id", "payload"]], use_container_width=True)
    except requests.RequestException as error:
        st.warning(f"事件记录暂不可用：{error_text(error)}")


page = st.sidebar.radio("导航", ["运行总览", "异常处置", "运营复盘"])

if page == "运行总览":
    st.title("运行总览")
    st.subheader("机坪车辆专用道路与运行轨迹")
    st.caption("外围环路 → 分区服务路 → 机位作业支路；蓝点编号对应机位，红色虚线表示封闭道路。")
    try:
        layout_data = get_layout_cached()
        initial_state = api_get("/state")
    except requests.RequestException as error:
        st.error(f"无法连接平台：{error_text(error)}")
        st.stop()
    nodes = layout_data["nodes"]
    node_options = [n["id"] for n in nodes if n["type"] in ("service_point", "depot", "checkpoint")]
    node_labels = {n["id"]: f'{n["label"]}（{n["id"]}）' for n in nodes}
    highlight_ids = st.multiselect("高亮车辆轨迹（最多 3 辆）", sorted(v["id"] for v in initial_state["vehicles"]), max_selections=3)
    show_aircraft = st.checkbox("叠加飞机滑行道参照", value=False)
    route_endpoints = None
    with st.expander("查看起终点行车路线"):
        enabled = st.checkbox("在图中显示路线", value=False)
        c1, c2 = st.columns(2)
        origin = c1.selectbox("路线起点", node_options, index=node_options.index("DEPOT_CATER"), format_func=node_labels.get)
        destination = c2.selectbox("路线终点", node_options, index=node_options.index("SP_20"), format_func=node_labels.get)
        if enabled:
            route_endpoints = (origin, destination)
        st.caption("路线避开封闭道路；交叉点仍采用停车 3 秒的仿真规则，尚未接入飞机占用信号。")
    live_overview(layout_data, highlight_ids, show_aircraft, route_endpoints)
    # Keep the form outside the timed fragment so typing is never interrupted.
    with st.form("create_task"):
        st.subheader("手动创建任务")
        st.session_state.setdefault("new_task_id", f"TASK_{uuid.uuid4().hex[:8]}")
        c1, c2, c3, c4 = st.columns(4)
        task_id = c1.text_input("任务 ID", key="new_task_id")
        task_type = c2.selectbox("类型", ["baggage_tow", "catering_supply", "inspection"])
        origin = c3.selectbox("起点", node_options, index=node_options.index("DEPOT_BAG_W"), format_func=node_labels.get)
        destination = c4.selectbox("终点", node_options, index=node_options.index("SP_01"), format_func=node_labels.get)
        priority = st.slider("优先级", 1, 5, 3)
        deadline = st.number_input("要求完成时间（从现在起，分钟）", min_value=1, max_value=1440, value=40)
        if st.form_submit_button("创建"):
            try:
                api_post("/tasks", json={"id": task_id, "type": task_type, "origin": origin,
                                         "destination": destination, "priority": priority, "deadline_offset_s": deadline * 60})
                st.success(f"任务 {task_id} 已保存，等待调度。创建下一条任务时请使用新的任务 ID。")
            except requests.RequestException as error:
                st.error(f"创建失败：{error_text(error)}")

elif page == "异常处置":
    st.title("异常处置")
    st.caption("未取货任务可自动转派；已取货或服务中的任务保留在原车，故障解除后续跑。")
    try:
        vehicles = api_get("/vehicles")
        layout_data = get_layout_cached()
    except requests.RequestException as error:
        st.error(error_text(error))
        st.stop()
    with st.form("inject_anomaly"):
        st.subheader("车辆异常演示")
        c1, c2 = st.columns(2)
        vehicle_id = c1.selectbox("车辆", [v["id"] for v in vehicles])
        labels = {"communication_lost": "通信中断", "position_quality_degraded": "定位质量下降"}
        kind = c2.selectbox("异常类型", list(labels), format_func=labels.get)
        if st.form_submit_button("注入车辆异常"):
            try:
                api_post("/anomalies/inject", anomaly_type=kind, vehicle_id=vehicle_id)
                st.success("异常已生效并保存。")
            except requests.RequestException as error:
                st.error(error_text(error))
    with st.form("close_road"):
        st.subheader("道路封闭")
        node_labels = {n["id"]: n["label"] for n in layout_data["nodes"]}
        segments = sorted({tuple(sorted((e["from"], e["to"]))) for e in layout_data["edges"]})
        edge = st.selectbox("封闭路段（双向）", segments,
                            format_func=lambda e: f"{node_labels[e[0]]} {e[0]} ↔ {node_labels[e[1]]} {e[1]}")
        reason = st.text_input("封闭原因", value="临时施工")
        st.caption("新路线避开该路段；已驶入的车辆原地等待，解除对应告警后恢复通行。")
        if st.form_submit_button("封闭道路"):
            try:
                api_post("/roads/closures", json={"from_node": edge[0], "to_node": edge[1], "reason": reason})
                st.success("道路已封闭并保存。可在下方对应道路告警中解除封闭。")
            except requests.RequestException as error:
                st.error(error_text(error))
    live_alerts()

else:
    st.title("运营复盘")
    @st.fragment(run_every=2)
    def live_metrics():
        try:
            m = api_get("/metrics")
        except requests.RequestException as error:
            st.error(error_text(error))
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("任务完成率", f"{m['completion_rate'] * 100:.1f}%")
        c2.metric("到期任务准时率", f"{m['on_time_rate'] * 100:.1f}%" if m['due_task_total'] else "暂无到期任务")
        c3.metric("告警解决率", f"{m['alert_resolution_rate'] * 100:.1f}%")
        c4.metric("逾期未完成", m['overdue_unfinished'])
        st.caption("准时率分母为仿真时间内已到截止时间的全部任务，包括未完成任务；告警解决率不代表人工干预率。")
        st.json(m)
    live_metrics()
