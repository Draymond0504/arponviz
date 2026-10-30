"""Streamlit 看板：现场运行、航班穿越、异常处置和离线实验回放。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# Streamlit may execute the file outside the repository working directory.
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apronops.experiments.replay import frame_tracks, load_replay

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

    for crossing in layout_data.get("crossings", []):
        radius = crossing["radius_m"]
        color = "#bf3f3f" if crossing["aircraft"] else "#c18423"
        fig.add_shape(type="circle", x0=crossing["x"]-radius, x1=crossing["x"]+radius,
                      y0=crossing["y"]-radius, y1=crossing["y"]+radius,
                      line=dict(color=color, width=2), fillcolor=color, opacity=0.25)
    occupied = [c for c in layout_data.get("crossings", []) if c["aircraft"] or c["waiting_aircraft"]]
    if occupied:
        fig.add_trace(go.Scatter(x=[c["x"] for c in occupied], y=[c["y"] for c in occupied],
            mode="markers+text", marker=dict(symbol="x", size=15, color="#bf3f3f"),
            text=[c["status"]+" · "+c["id"] for c in occupied], textposition="top center",
            name="飞机通行管制", hovertemplate="%{text}<extra></extra>"))

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
    current_layout = {**layout_data, "crossings": state.get("crossings", []), "edges": [
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
        st.dataframe(pd.DataFrame(tasks)[["id", "flight_id", "type", "status", "release_at", "deadline", "depends_on", "assigned_vehicle_id", "wait_reason"]], use_container_width=True)


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


def as_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def local_time(value):
    if not value:
        return ""
    return as_utc(value).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")


@st.fragment(run_every=2)
def live_flights():
    state = fetch_state()
    st.subheader("航班保障进度")
    if state["flights"]:
        df = pd.DataFrame(state["flights"])[["id", "stand_id", "arrival_at", "departure_at", "status", "completed_tasks", "total_tasks", "revision"]]
        for col in ("arrival_at", "departure_at"):
            df[col] = df[col].map(local_time)
        st.dataframe(df.rename(columns={"arrival_at": "预计到位（北京时间）", "departure_at": "预计离位（北京时间）"}), use_container_width=True)
        tasks = [t for t in state["tasks"] if t["flight_id"]]
        if tasks:
            st.dataframe(pd.DataFrame(tasks)[["flight_id", "type", "status", "release_at", "deadline", "depends_on", "wait_reason"]], use_container_width=True)
    else:
        st.info("尚未录入航班，可创建计划或载入合成演示。")
    st.subheader("交叉区通行状态")
    st.dataframe(pd.DataFrame(state["crossings"])[["id", "status", "aircraft", "waiting_aircraft", "vehicles", "radius_m"]], use_container_width=True)
    if state["occupancies"]:
        st.subheader("飞机占用窗口")
        df = pd.DataFrame(state["occupancies"])[["id", "aircraft_id", "crossing_id", "status", "predicted_start", "requested_start", "requested_end", "actual_start", "actual_end"]]
        for col in ("predicted_start", "requested_start", "requested_end", "actual_start", "actual_end"):
            df[col] = df[col].map(local_time)
        st.dataframe(df, use_container_width=True)
        st.caption("窗口时间为北京时间。actual_start / actual_end 是仿真实际授予的占用时间；区内有车辆时会延迟授予。")


page = st.sidebar.radio("导航", ["运行总览", "航班与穿越", "异常处置", "运营复盘", "实验回放"])

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
        st.caption("路线避开封闭道路；飞机获准占用期间车辆在交叉区外等待。路线距离不包含等待时间。")
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

elif page == "航班与穿越":
    st.title("航班与穿越")
    st.caption("合成运行模型：航班生成行李、配餐及保障后检查任务；检查在前两项完成后释放。飞机通行请求经冲突区清空后获准。")
    try:
        state = api_get("/state")
        layout_data = get_layout_cached()
    except requests.RequestException as error:
        st.error(error_text(error))
        st.stop()
    if st.button("载入航班与穿越演示"):
        try:
            api_post("/scenarios/turnaround-demo")
            st.success("已载入 1 个合成航班、3 项保障任务和 2 个穿越请求。重复点击不会重复创建。")
            state = api_get("/state")
        except requests.RequestException as error:
            st.error(error_text(error))
    stands = [n["id"] for n in layout_data["nodes"] if n["type"] == "service_point"]
    with st.form("flight_create"):
        st.subheader("创建航班计划")
        c1, c2 = st.columns(2)
        fid = c1.text_input("航班运行 ID", value="FLIGHT_001", help="同一航班不同日期请使用不同 ID。")
        stand = c2.selectbox("机位服务点", stands, index=stands.index("SP_20"))
        c1, c2 = st.columns(2)
        arrival_minutes = c1.number_input("距当前仿真时间到位（分钟）", min_value=0, max_value=1440, value=1)
        turnaround_minutes = c2.number_input("到位至离位（分钟）", min_value=1, max_value=1440, value=45)
        if st.form_submit_button("创建航班并生成保障任务"):
            try:
                now = as_utc(api_get("/state")["simulation_time"])
                arrival = now + timedelta(minutes=arrival_minutes)
                api_post("/flights", json={"id": fid, "stand_id": stand, "arrival_at": arrival.isoformat(),
                                         "departure_at": (arrival+timedelta(minutes=turnaround_minutes)).isoformat()})
                st.success("航班及保障任务已保存。")
            except requests.RequestException as error:
                st.error(error_text(error))
    if state["flights"]:
        with st.form("flight_delay"):
            st.subheader("更新航班计划")
            by_id = {f["id"]: f for f in state["flights"]}
            update_id = st.selectbox("待更新航班", list(by_id))
            delay = st.number_input("到位及离位顺延（分钟）", min_value=1, max_value=1440, value=5)
            st.caption("待执行任务同步更新时间窗；已开始作业保留位置和进度，已完成任务保留原截止时间。")
            if st.form_submit_button("提交航班延误"):
                f = by_id[update_id]
                try:
                    api_post("/flights", json={"id": f["id"], "stand_id": f["stand_id"],
                        "arrival_at": (as_utc(f["arrival_at"])+timedelta(minutes=delay)).isoformat(),
                        "departure_at": (as_utc(f["departure_at"])+timedelta(minutes=delay)).isoformat(),
                        "expected_revision": f["revision"]})
                    st.success("航班及关联任务时间窗已更新。")
                except requests.RequestException as error:
                    st.error(error_text(error))
    with st.expander("录入飞机穿越请求"):
        with st.form("occupancy_create"):
            st.session_state.setdefault("occupancy_id", f"CROSS_{uuid.uuid4().hex[:8]}")
            oid = st.text_input("占用请求 ID", key="occupancy_id")
            aircraft = st.text_input("航空器标识", value="AIRCRAFT_001")
            cid = st.selectbox("交叉点", [n["id"] for n in layout_data["nodes"] if n["type"] == "crossing"])
            lead = st.number_input("预计开始距当前仿真时间（秒）", min_value=10, max_value=86400, value=30)
            shift = st.number_input("申请通行相对预计时间推迟（秒）", min_value=0, max_value=3600, value=20)
            duration = st.number_input("占用持续时间（秒）", min_value=1, max_value=3600, value=45)
            st.caption("预计时间只用于对比；申请到达后禁止新车进入，区内车辆清空后飞机获得完整时长。")
            if st.form_submit_button("保存穿越请求"):
                try:
                    now = as_utc(api_get("/state")["simulation_time"])
                    predicted = now + timedelta(seconds=lead)
                    requested = predicted + timedelta(seconds=shift)
                    api_post("/crossings/occupancies", json={"id": oid, "aircraft_id": aircraft, "crossing_id": cid,
                        "predicted_start": predicted.isoformat(), "predicted_end": (predicted+timedelta(seconds=duration)).isoformat(),
                        "requested_start": requested.isoformat(), "requested_end": (requested+timedelta(seconds=duration)).isoformat()})
                    st.success("穿越请求已保存；创建下一条时请使用新的请求 ID。")
                except requests.RequestException as error:
                    st.error(error_text(error))
    live_flights()

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

elif page == "实验回放":
    st.title("实验回放")
    st.caption("读取实验日志，不推进当前运行现场。可拖动时间轴核对车辆、任务、交叉管制和封路情况。")
    results_root = Path(os.environ.get("APRONOPS_RESULTS_DIR", str(Path(__file__).resolve().parents[2] / "results/experiments")))
    local_files = sorted(results_root.glob("*/*/replay.json")) + sorted(results_root.glob("*/replay.json"))
    local_files = [p for p in local_files if p.resolve().is_relative_to(results_root.resolve())]
    uploaded = st.file_uploader("上传实验 replay.json", type=["json"])
    chosen = st.selectbox("本机实验结果", [None]+local_files,
                           format_func=lambda p: "请选择实验" if p is None else str(p.relative_to(results_root)))
    if uploaded is not None or chosen is not None:
        try:
            if uploaded is None and chosen.stat().st_size > 100*1024*1024:
                raise ValueError("回放文件超过 100 MB")
            data = load_replay(uploaded.getvalue() if uploaded is not None else chosen.read_bytes())
            manifest, frames = data["manifest"], data["frames"]
            st.write(f"场景 {manifest['scenario_id']} · 策略 {manifest['policy']} · 种子 {manifest['seed']}")
            if manifest["status"] != "completed":
                st.warning(f"此次实验状态为 {manifest['status']}，以下仅展示已保存的部分结果。")
            index = st.slider("回放帧", 0, len(frames)-1, 0) if len(frames)>1 else 0
            frame = frames[index]
            st.caption(f"仿真经过 {frame['elapsed_s']:.1f} 秒 · {frame['simulation_time']} UTC")
            closed = {frozenset((c['from_node'],c['to_node'])) for c in frame.get('closures',[]) if not c['reopened_at']}
            layout = {**data['layout'], 'crossings': frame.get('crossings',[]),
                      'edges': [{**e, 'closed': frozenset((e['from'],e['to'])) in closed} for e in data['layout']['edges']]}
            st.plotly_chart(build_topology_figure(layout, frame['vehicles'], frame_tracks(frames,index), []),
                            use_container_width=True, key="replay_map")
            st.subheader("当前帧任务")
            st.dataframe(pd.DataFrame(frame['tasks']), use_container_width=True)
            st.subheader("当前帧车辆")
            st.dataframe(pd.DataFrame([{k: v.get(k) for k in ('id','state','current_task_id','current_speed_mps','wait_reason')}
                                       for v in frame['vehicles']]), use_container_width=True)
            st.subheader("整次实验汇总（截至运行停止）")
            st.json(data['summary'])
            st.caption("未完成任务保留在完成率分母；延误下界包含未完成任务截至停止时刻的延误。回放轨迹按采样帧显示。")
        except (ValueError, OSError) as error:
            st.error(str(error))
    else:
        st.info("先运行批量实验，或上传云服务器生成的 replay.json。")

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
