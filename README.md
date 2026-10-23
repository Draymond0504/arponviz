# ApronOps

机场机坪自主作业协同与异常处置平台（仿真 / 数字孪生 MVP）。

**边界声明**：本项目是面向封闭机坪场景的仿真与运营协同平台，**不**声称可直接部署到真实机场机坪。真实机坪涉及适航、机场运行规程、通信与安全认证，个人项目不越过这些边界；所有阈值、时长、坐标均为模拟示意值。

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
pytest                                   # 单元测试
uvicorn apronops.api.main:app --reload   # 启动 API（含后台模拟循环），默认 :8000
streamlit run apronops/dashboard/app.py  # 另开终端启动看板，默认 :8501
```

### Docker Compose

```bash
docker compose up --build
```

- API: http://localhost:8000/docs
- 看板: http://localhost:8501

## 目录结构

见项目根目录下的模块划分：`apronops/models.py`（数据模型）、`state_machine.py`（状态机）、`scheduler.py`（调度）、`anomaly.py`（异常检测）、`simulator.py`（模拟循环）、`storage.py`（SQLite 持久化）、`metrics.py`（KPI）、`api/`（FastAPI）、`dashboard/`（Streamlit）。

## 数据说明

当前所有数据均为**模拟数据**，位于 `data/*.yaml`：

| 文件 | 内容 | 对应真实数据来源（生产环境，本项目不接入） |
|---|---|---|
| `apron_layout.yaml` | 机坪作业点坐标与连接边 | 机场 AIP/平面图、GIS 数据 |
| `vehicles_seed.yaml` | 车辆类型、初始位置、速度、电量 | 车辆厂商规格书 |
| `tasks_seed.yaml` | 模拟任务（类型、起终点、优先级、截止时间） | 航班时刻表 / FIDS / 地面服务工单系统 |

保障作业标准时长（`simulator.py` 中 `SERVICE_DURATION_S`）、异常检测阈值（`anomaly.py`）、安全余量等同样为示意常数，如需替换为真实运营数据，需机场运行方审批并对接相应系统，超出本项目范围。

## 外部依赖

无需安装任何外部仿真软件（不需要 ROS2/Gazebo/SUMO/AnyLogic）。全部逻辑为纯 Python 自实现的离散时间步模拟循环，`pip install -r requirements.txt` 即可运行整个项目。

后续如强化"具身智能"叙事，可选接入 ROS2 + Nav2 + Gazebo（需 Ubuntu/WSL2 环境）；如需更真实的路网/交通仿真，可选接入 SUMO（`pip install eclipse-sumo`）。两者均不在当前骨架范围内。


## 独立维护

GitHub 仓库：https://github.com/Draymond0504/arponviz

```bash
git clone git@github.com:Draymond0504/arponviz.git
cd arponviz
```

本仓库从 `Draymond0504/airport-marl` 拆出，保留初始导入提交供追溯。
当前工作树仅包含 ApronOps 平台，平台使用自身车辆仿真与规则调度。
算法和论文研究代码继续维护于 https://github.com/Draymond0504/airport-marl 。

使用 Python 3.11 或 3.12，先创建本地环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests -q
```

前端入口为 `apronops/dashboard/app.py`，后端入口为
`apronops/api/main.py`。默认前端连接 `http://localhost:8000`；
通过 `APRONOPS_API_BASE` 可指定后端地址。
`APRONOPS_DB_PATH` 指定 SQLite 数据库位置，数据文件不加入 Git。

修改前端后需同时启动 API 和 Streamlit 核对页面；涉及业务逻辑时运行
`pytest tests -q`。两套工程尚未通过 API 接入，不依赖 MAPPO 模型权重。
